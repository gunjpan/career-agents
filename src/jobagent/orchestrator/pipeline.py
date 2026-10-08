from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from jobagent.adapters.ats.base import ATSAdapter, Enrichable
from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.models.criteria import Criteria
from jobagent.models.fetch_policy import FetchPolicy
from jobagent.models.job import JOBS_HEADERS, Job, JobState
from jobagent.orchestrator.dedupe import dedupe_key, job_id
from jobagent.orchestrator.filters import apply_hard_filters
from jobagent.orchestrator.states import transition
from jobagent.storage.base import Storage


@dataclass
class CompanyResult:
    company: str
    fetched: int = 0
    added: int = 0
    duplicates: int = 0
    enrich_failed: int = 0  # detail fetch failed; job is retried next run, nothing stored
    rejected: Counter = field(default_factory=Counter)  # keyed by filter: title/location/age
    flagged: Counter = field(default_factory=Counter)
    error: str = ""
    cooldown: str = ""  # set when the company was skipped because it was fetched recently


@dataclass
class ProgressEvent:
    """Where the run is, for a progress display. total is None while it is unknown."""

    company: str
    stage: str
    done: int
    total: int | None
    company_index: int
    company_count: int


@dataclass
class RunSummary:
    results: list[CompanyResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # companies not active


def run_pipeline(
    storage: Storage,
    adapters: dict[str, ATSAdapter],
    criteria: Criteria,
    now: datetime,
    on_progress: Callable[[ProgressEvent], None] = lambda event: None,
    policy: FetchPolicy | None = None,
    force: bool = False,
    only: set[str] | None = None,
) -> RunSummary:
    """new -> filtered for every fresh posting of every active company. No LLM calls."""
    storage.ensure_headers("Companies", COMPANIES_HEADERS)
    storage.ensure_headers("Jobs", JOBS_HEADERS)
    companies = [Company.from_record(r) for r in storage.read_records("Companies") if r.get("name")]
    existing = storage.read_records("Jobs")
    seen_keys = {r["dedupe_key"] for r in existing}
    seen_ids = {(r["ats"], r["external_id"]) for r in existing}

    summary = RunSummary()
    active = [c for c in companies if c.status == "active"]
    summary.skipped = [f"{c.name} ({c.status})" for c in companies if c.status != "active"]
    for index, company in enumerate(active, start=1):

        def report(stage: str, done: int = 0, total: int | None = None, c=company, i=index) -> None:
            on_progress(ProgressEvent(c.name, stage, done, total, i, len(active)))

        if only is not None and company.name.lower() not in only:
            continue
        result = CompanyResult(company.name)
        wait = (
            policy.wait_remaining(company.ats, company.last_fetched, now)
            if policy and not force
            else None
        )
        if wait:  # fetched too recently: leave the company alone (politeness; avoids being flagged as a bot)
            ago = (now - company.last_fetched).total_seconds() / 3600
            result.cooldown = f"fetched {ago:.1f}h ago; next in {wait.total_seconds() / 3600:.1f}h"
            summary.results.append(result)
            continue
        summary.results.append(result)
        adapter = adapters.get(company.ats)
        if adapter is None:
            result.error = f"no adapter for ATS {company.ats!r}"
            continue
        report("fetching postings")
        # Stamped before fetching, so a failing board is not hammered by every rerun either.
        storage.update_records(
            "Companies", "name", {company.name: {"last_fetched": now.isoformat(timespec="seconds")}}
        )
        try:
            postings = adapter.fetch(company)
        except Exception as e:  # noqa: BLE001 - one company's bad response must not stop the run
            result.error = f"fetch failed: {type(e).__name__}: {e}"
            continue

        enrich = adapter.enrich if isinstance(adapter, Enrichable) else None
        new_jobs: list[Job] = []
        result.fetched = len(postings)
        for done, posting in enumerate(postings):
            report("filtering, fetching details", done, len(postings))
            verdict = apply_hard_filters(posting, criteria, now)
            if not verdict.passed:
                result.rejected[verdict.bucket] += 1
                continue
            # Cheap duplicate check by ATS id first, so a daily run doesn't re-fetch details.
            if (posting.ats, posting.external_id) in seen_ids:
                result.duplicates += 1
                continue
            if enrich:
                try:
                    posting = enrich(posting)
                except (httpx.HTTPError, ValueError, KeyError):
                    result.enrich_failed += 1
                    continue
                verdict = apply_hard_filters(posting, criteria, now)  # richer data, re-check
                if not verdict.passed:
                    result.rejected[verdict.bucket] += 1
                    continue
            key = dedupe_key(posting)
            if key in seen_keys:
                result.duplicates += 1
                continue
            seen_keys.add(key)
            seen_ids.add((posting.ats, posting.external_id))
            job = Job(
                job_id=job_id(key),
                dedupe_key=key,
                state=JobState.NEW,
                flags=verdict.flags,
                first_seen=now,
                posting=posting,
            )
            new_jobs.append(transition(job, JobState.FILTERED))
            result.flagged.update(verdict.flags)
        report("writing rows", len(postings), len(postings))
        storage.append_records("Jobs", JOBS_HEADERS, [j.to_record() for j in new_jobs])
        result.added = len(new_jobs)
    return summary
