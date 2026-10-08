from dataclasses import dataclass
from datetime import UTC, datetime

from jobagent.agents.tailor import Assessment
from jobagent.agents.verifier import Verifier, decide
from jobagent.evals.models import Targets, VerifierCase
from jobagent.evals.report import EvalReport, Metric
from jobagent.llm.base import LLMError
from jobagent.models.job import Job, JobState, RawPosting
from jobagent.models.tailoring import Limits
from jobagent.orchestrator.checks import check_tailor_output
from jobagent.orchestrator.dedupe import dedupe_key


@dataclass
class VerifierCaseResult:
    case: VerifierCase
    layer: str  # "code" | "verifier" | "none" (nothing stopped it) | "error"
    detail: str = ""
    cost_usd: float = 0.0

    @property
    def outcome(self) -> str:
        if self.layer == "error":
            return "error"
        stopped = self.layer in {"code", "verifier"}
        if self.case.kind == "corrupt":
            return f"caught_by_{self.layer}" if stopped else "ESCAPED"
        return "false_alarm" if stopped else "passed"


def _job(case: VerifierCase) -> Job:
    p = RawPosting(company="Example Corp", ats="eval", external_id=case.id, title="Director",
                   url="https://example.invalid/" + case.id, description=case.posting)  # fmt: skip
    return Job(job_id=case.id, dedupe_key=dedupe_key(p), state=JobState.FILTERED,
               first_seen=datetime.now(UTC), posting=p)  # fmt: skip


def run_verifier_eval(
    verifier: Verifier,
    resume: dict,
    cases: list[VerifierCase],
    targets: Targets,
    *,
    limits: Limits | None = None,
    max_cost_usd: float = 2.0,
) -> EvalReport:
    """Replays the real defence order: free code checks first, the Verifier model only for drafts
    the code cannot judge (like production, so an invented number never costs a model call)."""
    results: list[VerifierCaseResult] = []
    spent = 0.0
    for case in cases:
        if spent >= max_cost_usd:
            break
        problems = check_tailor_output(
            case.draft, resume, limits or Limits(), enforce_bullet_limits=False
        )
        if problems:
            results.append(VerifierCaseResult(case, "code", "; ".join(problems)[:140]))
            continue
        try:
            run = verifier.run(
                _job(case), Assessment(case.level, "engineering", [], []), case.draft
            )
        except LLMError as e:
            results.append(VerifierCaseResult(case, "error", str(e)[:120]))
            continue
        spent += run.cost_usd
        decision = decide(run.output)
        results.append(
            VerifierCaseResult(
                case,
                "none" if decision.passed else "verifier",
                "; ".join(decision.problems)[:140],
                run.cost_usd,
            )
        )
    return summarise(results, targets, spent, verifier)


def summarise(
    results: list[VerifierCaseResult], targets: Targets, cost: float, verifier: Verifier
) -> EvalReport:
    corrupt = [r for r in results if r.case.kind == "corrupt"]
    clean = [r for r in results if r.case.kind == "clean"]
    escaped = [r for r in corrupt if r.outcome == "ESCAPED"]
    alarms = [r for r in clean if r.outcome == "false_alarm"]
    errors = [r for r in results if r.outcome == "error"]
    layer_ok = sum(r.layer == r.case.expected_layer for r in results if r.outcome != "error")

    failures = [f"{r.case.id}: ESCAPED, nothing flagged ({r.case.corruption})" for r in escaped]
    failures += [f"{r.case.id}: clean draft rejected ({r.detail})" for r in alarms]
    failures += [f"{r.case.id}: model call failed ({r.detail})" for r in errors]
    metrics = [
        Metric(
            "verifier_escape_rate",
            len(escaped) / (len(corrupt) or 1),
            targets.verifier_escape_rate_max,
            "<=",
        ),
        Metric(
            "verifier_false_alarm_rate",
            len(alarms) / (len(clean) or 1),
            targets.verifier_false_alarm_max,
            "<=",
        ),
        Metric(
            "caught_by_code",
            sum(r.outcome == "caught_by_code" for r in corrupt) / (len(corrupt) or 1),
        ),
        Metric(
            "caught_by_verifier",
            sum(r.outcome == "caught_by_verifier" for r in corrupt) / (len(corrupt) or 1),
        ),
        Metric("right_layer_caught_it", layer_ok / (len(results) - len(errors) or 1)),
    ]
    return EvalReport(
        "verifier",
        verifier.config.verifier.model,
        verifier.prompt.version,
        len(results),
        metrics,
        cost,
        failures,
    )
