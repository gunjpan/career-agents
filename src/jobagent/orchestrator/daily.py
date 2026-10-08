"""The scheduled job: onboard new companies, fetch and filter, score. Plain orchestration.

Output is written for a PUBLIC log: counts and costs only, never company names, job titles or
resume text, because a list of the companies you track would reveal your job search.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from jobagent.agents.scorer import Scorer
from jobagent.models.criteria import Criteria
from jobagent.models.fetch_policy import FetchPolicy
from jobagent.models.scoring import ScoringConfig
from jobagent.onboarding.onboard import Onboarder
from jobagent.orchestrator.onboarding import run_onboarding
from jobagent.orchestrator.pipeline import RunSummary, run_pipeline
from jobagent.orchestrator.scoring import RUNS_HEADERS, ScoringSummary, log_run, run_scoring
from jobagent.storage.base import Storage


@dataclass
class DailySummary:
    started: datetime
    finished: datetime | None = None
    onboarded: Counter[str] = field(default_factory=Counter)  # status -> companies
    fetch: RunSummary = field(default_factory=RunSummary)
    scoring: ScoringSummary | None = None
    scoring_note: str = ""

    # --- derived, all name-free ------------------------------------------------------------
    @property
    def attempted(self) -> list:
        return [r for r in self.fetch.results if not r.cooldown]

    @property
    def errors(self) -> Counter[str]:
        """Failures by kind, e.g. {'HTTPStatusError': 2}: no company names."""
        kinds: Counter[str] = Counter()
        for r in self.attempted:
            if r.error:
                head = r.error.removeprefix("fetch failed: ")
                kinds[head.split(":")[0].strip() if ":" in head else head[:40]] += 1
        return kinds

    @property
    def rejected(self) -> Counter[str]:
        """Rejections by filter family. Rule names can contain company names, so they are merged."""
        out: Counter[str] = Counter()
        for r in self.fetch.results:
            for bucket, n in r.rejected.items():
                family = (
                    bucket.split(":")[0] if bucket.startswith(("rule:", "include:")) else bucket
                )
                out[
                    "rules"
                    if family == "rule"
                    else "include-rules"
                    if family == "include"
                    else family
                ] += n
        return out

    @property
    def exit_code(self) -> int:
        """Non-zero (so GitHub emails you) only when every company we tried failed to fetch."""
        return 1 if self.attempted and sum(self.errors.values()) == len(self.attempted) else 0

    def to_markdown(self) -> str:
        f, sc = self.fetch, self.scoring
        fetched = [r for r in self.attempted if not r.error]
        lines = ["## Daily run", ""]
        if self.onboarded:
            lines.append(
                f"- **Onboarding:** {', '.join(f'{n} {s}' for s, n in sorted(self.onboarded.items()))}"
            )
        lines.append(
            f"- **Companies:** {len(fetched)} fetched, {sum(1 for r in f.results if r.cooldown)} skipped (cooldown), "
            f"{sum(self.errors.values())} failed, {len(f.skipped)} not active"
        )
        if self.errors:
            lines.append(
                f"- **Fetch errors by kind:** {', '.join(f'{k} x{n}' for k, n in self.errors.items())}"
            )
        added = sum(r.added for r in f.results)
        lines.append(
            f"- **Postings:** {sum(r.fetched for r in f.results)} seen, {added} new, "
            f"{sum(r.duplicates for r in f.results)} already known, {sum(self.rejected.values())} filtered out"
        )
        if self.rejected:
            lines.append(
                f"- **Filtered out by:** {', '.join(f'{k} {n}' for k, n in sorted(self.rejected.items()))}"
            )
        if sc is not None:
            lines.append(
                f"- **Scoring:** {sc.scored} scored, {sc.shortlisted} shortlisted, {sc.failed} failed, "
                f"{sc.remaining} left for next run; ${sc.cost_usd:.4f} "
                f"({sc.input_tokens} in / {sc.output_tokens} out tokens)"
            )
            if sc.stopped:
                lines.append(f"- **Scoring stopped early:** {sc.stopped}")
        elif self.scoring_note:
            lines.append(f"- **Scoring:** {self.scoring_note}")
        return "\n".join(lines) + "\n"


def log_fetch_run(
    storage: Storage, summary: RunSummary, started: datetime, finished: datetime
) -> None:
    results = summary.results
    attempted = [r for r in results if not r.cooldown]
    notes = f"{len(attempted)} fetched, {len(results) - len(attempted)} cooldown, {sum(1 for r in attempted if r.error)} failed"
    storage.append_records(
        "Runs",
        RUNS_HEADERS,
        [{
            "run_id": f"{started:%Y%m%dT%H%M%SZ}-run", "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"), "command": "run",
            "items": str(sum(r.added for r in results)), "cost_usd": "0.000000", "notes": notes,
        }],
    )  # fmt: skip


def run_daily(
    storage: Storage,
    adapters: dict,
    criteria: Criteria,
    policy: FetchPolicy,
    *,
    now: datetime,
    clock,
    onboarder: Onboarder | None = None,
    scorer: Scorer | None = None,
    scoring_config: ScoringConfig | None = None,
    prompt_version: int = 0,
) -> DailySummary:
    """onboard pending -> fetch + filter -> score. `clock` returns the current time (testable)."""
    summary = DailySummary(started=now)
    if onboarder is not None and any(
        r.get("status") == "pending" for r in storage.read_records("Companies")
    ):
        for _, result in run_onboarding(storage, onboarder):
            summary.onboarded[result.status] += 1

    summary.fetch = run_pipeline(storage, adapters, criteria, now, policy=policy)
    log_fetch_run(storage, summary.fetch, now, clock())

    if scorer is None or scoring_config is None:
        summary.scoring_note = "skipped (no Anthropic key or scoring disabled)"
    else:
        started = clock()
        summary.scoring = run_scoring(storage, scorer, scoring_config)
        log_run(storage, command="score", started_at=started, finished_at=clock(), summary=summary.scoring, config=scoring_config, prompt_version=prompt_version)  # fmt: skip
    summary.finished = clock()
    return summary
