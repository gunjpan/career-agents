from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jobagent.agents.scorer import Scorer
from jobagent.evals.models import LEVELS, ScorerCase, Targets
from jobagent.evals.report import EvalReport, Metric
from jobagent.llm.base import LLMError
from jobagent.models.job import Job, JobState, RawPosting
from jobagent.models.scoring import ScoreResult
from jobagent.orchestrator.dedupe import dedupe_key


@dataclass
class ScorerCaseResult:
    case: ScorerCase
    runs: list[ScoreResult] = field(default_factory=list)
    error: str = ""


def _job(case: ScorerCase) -> Job:
    p = RawPosting(company=case.company, ats="eval", external_id=case.id, title=case.title,
                   url="https://example.invalid/" + case.id, description=case.description)  # fmt: skip
    return Job(job_id=case.id, dedupe_key=dedupe_key(p), state=JobState.FILTERED,
               first_seen=datetime.now(UTC), posting=p)  # fmt: skip


def distance(label: str, got: str) -> int:
    return abs(LEVELS.index(label) - LEVELS.index(got))


def run_scorer_eval(
    scorer: Scorer,
    cases: list[ScorerCase],
    targets: Targets,
    *,
    repeats: int = 1,
    max_cost_usd: float = 2.0,
) -> EvalReport:
    results: list[ScorerCaseResult] = []
    spent = 0.0
    stopped = ""
    for case in cases:
        if spent >= max_cost_usd:
            stopped = f"spend cap ${max_cost_usd:.2f} reached after {len(results)} cases"
            break
        res = ScorerCaseResult(case)
        results.append(res)
        for _ in range(repeats):
            try:
                run = scorer.score(_job(case))
            except LLMError as e:
                res.error = str(e)[:120]
                break
            res.runs.append(run)
            spent += run.cost_usd
    return score_results(results, targets, spent, scorer, stopped)


def score_results(
    results: list[ScorerCaseResult],
    targets: Targets,
    cost: float,
    scorer: Scorer,
    stopped: str = "",
) -> EvalReport:
    runs = [(r.case, run) for r in results for run in r.runs]
    n = len(runs) or 1
    exact = sum(run.output.real_level == c.label_level for c, run in runs)
    within = sum(distance(c.label_level, run.output.real_level) <= 1 for c, run in runs)
    flag = sum(run.output.title_matches_level == c.title_matches_level for c, run in runs)
    steady = [
        len({run.output.real_level for run in r.runs}) == 1 for r in results if len(r.runs) > 1
    ]

    failures = [
        f"{c.id}: expected {c.label_level}, got {run.output.real_level} ({run.output.level_confidence} confidence)"
        for c, run in runs if run.output.real_level != c.label_level
    ]  # fmt: skip
    failures += [f"{r.case.id}: model call failed ({r.error})" for r in results if r.error]
    metrics = [
        Metric("scorer_level_accuracy", exact / n, targets.scorer_level_accuracy),
        Metric("scorer_within_one_level", within / n, targets.scorer_within_one_level),
        Metric("title_mismatch_flag_accuracy", flag / n),  # informational
    ]
    if steady:
        metrics.append(Metric("run_to_run_agreement", sum(steady) / len(steady)))  # informational
    confusion = Counter(
        (c.label_level, run.output.real_level)
        for c, run in runs
        if run.output.real_level != c.label_level
    )
    notes = "; ".join(
        filter(None, [stopped, *(f"{a}->{b} x{k}" for (a, b), k in confusion.items())])
    )
    return EvalReport(
        "scorer",
        scorer.config.model,
        scorer.prompt.version,
        len(results),
        metrics,
        cost,
        failures,
        notes,
    )
