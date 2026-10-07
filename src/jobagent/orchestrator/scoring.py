from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from jobagent.agents.scorer import Scorer
from jobagent.llm.base import LLMError
from jobagent.models.job import JOBS_HEADERS, Job, JobState
from jobagent.models.scoring import ScoreResult, ScoringConfig
from jobagent.orchestrator.states import transition
from jobagent.storage.base import Storage

FLUSH_EVERY = 10  # rows written per Sheets request; also written on error or Ctrl+C

RUNS_HEADERS = [
    "run_id",
    "started_at",
    "finished_at",
    "command",
    "items",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cost_usd",
    "model",
    "prompt_version",
    "notes",
]


@dataclass
class ScoringSummary:
    scored: int = 0
    shortlisted: int = 0
    failed: int = 0
    remaining: int = 0  # filtered jobs not attempted (limit or spend cap)
    stopped: str = ""  # why the run stopped early, if it did
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    by_level: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)


def is_shortlisted(result: ScoreResult, config: ScoringConfig) -> bool:
    """The shortlist decision lives in code so thresholds change without touching the prompt."""
    rule, out = config.shortlist, result.output
    return (
        out.core_domain_covered
        and out.fit_score >= rule.min_fit
        and out.real_level in rule.levels
        and out.function in rule.functions
    )


def run_scoring(
    storage: Storage,
    scorer: Scorer,
    config: ScoringConfig,
    *,
    limit: int | None = None,
    only: set[str] | None = None,  # score just these job_ids (still only if `filtered`)
    on_progress: Callable[[int, int, str], None] = lambda done, total, title: None,
) -> ScoringSummary:
    """filtered -> scored (-> shortlisted). Only `filtered` jobs are scored, so rerunning is free."""
    storage.ensure_headers("Jobs", JOBS_HEADERS)
    todo = [
        Job.from_record(r)
        for r in storage.read_records("Jobs")
        if r["state"] == "filtered" and (only is None or r["job_id"] in only)
    ]
    batch = todo[:limit] if limit is not None else todo

    summary = ScoringSummary()
    pending: dict[str, dict[str, str]] = {}

    def flush() -> None:
        if pending:
            storage.update_records("Jobs", "job_id", dict(pending))
            pending.clear()

    attempted = 0
    try:
        for job in batch:
            if summary.cost_usd >= config.max_run_cost_usd:
                summary.stopped = f"spend cap ${config.max_run_cost_usd:.2f} reached"
                break
            attempted += 1
            on_progress(attempted - 1, len(batch), job.posting.title)
            try:
                result = scorer.score(job)
            except LLMError as e:
                summary.failed += 1
                summary.errors.append(f"{job.posting.company} / {job.posting.title}: {e}")
                continue  # stays `filtered`; retried next run

            scored = transition(job.model_copy(update={"score": result}), JobState.SCORED)
            if is_shortlisted(result, config):
                scored = transition(scored, JobState.SHORTLISTED)
                summary.shortlisted += 1
            pending[job.job_id] = scored.score_record()

            summary.scored += 1
            summary.cost_usd += result.cost_usd
            summary.input_tokens += result.input_tokens
            summary.output_tokens += result.output_tokens
            summary.cache_read_tokens += result.cache_read_tokens
            summary.by_level[result.output.real_level] += 1
            if len(pending) >= FLUSH_EVERY:
                flush()
    finally:
        flush()  # never lose scores already paid for
    summary.remaining = len(todo) - summary.scored - summary.failed
    return summary


def log_run(
    storage: Storage,
    *,
    command: str,
    started_at: datetime,
    finished_at: datetime,
    summary: ScoringSummary,
    config: ScoringConfig,
    prompt_version: int,
) -> None:
    storage.append_records(
        "Runs",
        RUNS_HEADERS,
        [
            {
                "run_id": started_at.strftime("%Y%m%dT%H%M%SZ"),
                "started_at": started_at.isoformat(timespec="seconds"),
                "finished_at": finished_at.isoformat(timespec="seconds"),
                "command": command,
                "items": str(summary.scored),
                "input_tokens": str(summary.input_tokens),
                "output_tokens": str(summary.output_tokens),
                "cache_read_tokens": str(summary.cache_read_tokens),
                "cost_usd": f"{summary.cost_usd:.6f}",
                "model": config.model,
                "prompt_version": str(prompt_version),
                "notes": "; ".join(filter(None, [summary.stopped, f"{summary.failed} failed"])),
            }
        ],
    )
