from typing import Literal

from jobagent.models.job import Job, JobState

Actor = Literal["orchestrator", "human"]

S = JobState
ALLOWED: dict[JobState, set[JobState]] = {
    S.NEW: {S.FILTERED, S.CLOSED},
    S.FILTERED: {S.SCORED, S.CLOSED},
    S.SCORED: {S.SHORTLISTED, S.CLOSED},
    S.SHORTLISTED: {S.APPROVED, S.CLOSED},
    S.APPROVED: {S.TAILORED, S.CLOSED},
    S.TAILORED: {S.VERIFIED, S.TAILORED, S.CLOSED},  # TAILORED -> TAILORED: Verifier send-back
    S.VERIFIED: {S.READY, S.CLOSED},
    S.READY: {S.SUBMITTED, S.CLOSED},
    S.SUBMITTED: {S.INTERVIEW, S.REJECTED, S.CLOSED},  # never back to APPROVED
    S.INTERVIEW: {S.REJECTED, S.CLOSED},
    S.REJECTED: set(),
    S.CLOSED: set(),
}
HUMAN_ONLY = {S.APPROVED, S.SUBMITTED}


class InvalidTransition(Exception):
    pass


def transition(job: Job, to: JobState, actor: Actor = "orchestrator") -> Job:
    """The only place a job's state changes. Returns a new Job; the input is untouched."""
    if to not in ALLOWED[job.state]:
        raise InvalidTransition(f"{job.state.value} -> {to.value} is not allowed")
    if to in HUMAN_ONLY and actor != "human":
        raise InvalidTransition(f"only a human can move a job to {to.value}")
    return job.model_copy(update={"state": to})
