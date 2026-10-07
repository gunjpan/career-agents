import pytest

from jobagent.models.job import Job, JobState
from jobagent.orchestrator.dedupe import dedupe_key, job_id
from jobagent.orchestrator.states import InvalidTransition, transition

from .conftest import NOW, posting


def make_job(state: JobState) -> Job:
    p = posting()
    return Job(job_id="x", dedupe_key=dedupe_key(p), state=state, first_seen=NOW, posting=p)


def test_orchestrator_moves_new_to_filtered():
    assert transition(make_job(JobState.NEW), JobState.FILTERED).state == JobState.FILTERED


def test_cannot_skip_states():
    with pytest.raises(InvalidTransition):
        transition(make_job(JobState.NEW), JobState.SCORED)


def test_only_human_approves_or_submits():
    with pytest.raises(InvalidTransition):
        transition(make_job(JobState.SHORTLISTED), JobState.APPROVED)
    assert (
        transition(make_job(JobState.SHORTLISTED), JobState.APPROVED, actor="human").state
        == JobState.APPROVED
    )
    with pytest.raises(InvalidTransition):
        transition(make_job(JobState.READY), JobState.SUBMITTED)


def test_submitted_never_returns_to_approved():
    with pytest.raises(InvalidTransition):
        transition(make_job(JobState.SUBMITTED), JobState.APPROVED, actor="human")


def test_dedupe_key_ignores_case_and_punctuation():
    a = posting(title="Director, Engineering (Platform)", locations=["Toronto, ON"])
    b = posting(title="director engineering platform", locations=["TORONTO ON"])
    assert dedupe_key(a) == dedupe_key(b)
    assert job_id(dedupe_key(a)) == job_id(dedupe_key(b))


def test_dedupe_key_differs_by_location_and_company():
    base = posting()
    assert dedupe_key(base) != dedupe_key(posting(locations=["Vancouver, BC"]))
    assert dedupe_key(base) != dedupe_key(posting(company="Other"))
