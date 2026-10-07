from datetime import UTC, datetime

import pytest

from jobagent.adapters.ats.ashby import AshbyAdapter
from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.orchestrator.pipeline import run_pipeline
from jobagent.storage.csv_store import CsvStorage

from .conftest import fixture_client


@pytest.fixture
def criteria(criteria):
    """Pipeline mechanics, not personal filter choices: keep locations and age, drop the rules."""
    return criteria.model_copy(update={"rules": []})


def seeded_storage(tmp_path, *companies: Company) -> CsvStorage:
    storage = CsvStorage(str(tmp_path))
    storage.append_records("Companies", COMPANIES_HEADERS, [c.to_record() for c in companies])
    return storage


def test_pipeline_filters_dedupes_and_is_idempotent(tmp_path, criteria):
    # Fixture date is "now" so every recorded posting is within the 30-day window.
    now = datetime(2026, 9, 28, tzinfo=UTC)
    ws = Company(name="Wealthsimple", ats="ashby", board_id="wealthsimple", status="active")
    storage = seeded_storage(tmp_path, ws)
    adapters = {"ashby": AshbyAdapter(fixture_client("ashby_wealthsimple"))}

    first = run_pipeline(storage, adapters, criteria, now)
    result = first.results[0]
    assert result.fetched == 6
    assert result.added + sum(result.rejected.values()) + result.duplicates == 6
    assert result.added >= 1
    jobs = storage.read_records("Jobs")
    assert len(jobs) == result.added
    assert {j["state"] for j in jobs} == {"filtered"}
    assert all(j["flags"] == "" or "unknown" in j["flags"] for j in jobs)

    second = run_pipeline(storage, adapters, criteria, now)
    assert second.results[0].added == 0
    assert second.results[0].duplicates == result.added
    assert len(storage.read_records("Jobs")) == result.added


def test_inactive_company_skipped_and_unknown_ats_reported(tmp_path, criteria, now):
    pending = Company(name="Bank", ats="workday", board_id="x", status="needs_review")
    active_unknown = Company(name="Other", ats="workday", board_id="y", status="active")
    storage = seeded_storage(tmp_path, pending, active_unknown)

    summary = run_pipeline(storage, {}, criteria, now)
    assert summary.skipped == ["Bank (needs_review)"]
    assert "no adapter" in summary.results[0].error
