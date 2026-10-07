from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from jobagent.adapters.ats.base import ATSAdapter
from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.models.criteria import Criteria
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
    rejected: Counter = field(default_factory=Counter)  # keyed by filter: title/location/age
    flagged: Counter = field(default_factory=Counter)
    error: str = ""


@dataclass
class RunSummary:
    results: list[CompanyResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # companies not active


def run_pipeline(
    storage: Storage,
    adapters: dict[str, ATSAdapter],
    criteria: Criteria,
    now: datetime,
) -> RunSummary:
    """new -> filtered for every fresh posting of every active company. No LLM calls."""
    storage.ensure_headers("Companies", COMPANIES_HEADERS)
    storage.ensure_headers("Jobs", JOBS_HEADERS)
    companies = [Company.from_record(r) for r in storage.read_records("Companies") if r.get("name")]
    seen = {r["dedupe_key"] for r in storage.read_records("Jobs")}

    summary = RunSummary()
    for company in companies:
        if company.status != "active":
            summary.skipped.append(f"{company.name} ({company.status})")
            continue
        result = CompanyResult(company.name)
        summary.results.append(result)
        adapter = adapters.get(company.ats)
        if adapter is None:
            result.error = f"no adapter for ATS {company.ats!r}"
            continue
        try:
            postings = adapter.fetch(company)
        except httpx.HTTPError as e:
            result.error = f"fetch failed: {e}"
            continue

        new_jobs: list[Job] = []
        result.fetched = len(postings)
        for posting in postings:
            verdict = apply_hard_filters(posting, criteria, now)
            if not verdict.passed:
                result.rejected[verdict.reason.split(":")[0]] += 1
                continue
            key = dedupe_key(posting)
            if key in seen:
                result.duplicates += 1
                continue
            seen.add(key)
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
        storage.append_records("Jobs", JOBS_HEADERS, [j.to_record() for j in new_jobs])
        result.added = len(new_jobs)
    return summary
