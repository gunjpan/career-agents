from datetime import UTC, datetime

import pytest

from jobagent.adapters.ats.ashby import AshbyAdapter
from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.orchestrator.pipeline import run_pipeline
from jobagent.storage.base import StorageError
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


def test_new_trailing_column_is_added_to_an_existing_tab(tmp_path):
    storage = CsvStorage(str(tmp_path))
    storage.append_records("Jobs", ["a", "b"], [{"a": "1", "b": "2"}])

    storage.append_records("Jobs", ["a", "b", "c"], [{"a": "3", "b": "4", "c": "5"}])
    assert storage.read_records("Jobs") == [
        {"a": "1", "b": "2", "c": ""},
        {"a": "3", "b": "4", "c": "5"},
    ]


def test_renamed_or_reordered_columns_still_raise(tmp_path):
    storage = CsvStorage(str(tmp_path))
    storage.append_records("Jobs", ["a", "b"], [{"a": "1", "b": "2"}])
    with pytest.raises(StorageError):
        storage.ensure_headers("Jobs", ["b", "a"])
    with pytest.raises(StorageError):
        storage.ensure_headers("Jobs", ["a", "x", "c"])


def test_progress_events_report_each_company_and_stage(tmp_path, criteria):
    now = datetime(2026, 9, 28, tzinfo=UTC)
    ws = Company(name="Wealthsimple", ats="ashby", board_id="wealthsimple", status="active")
    paused = Company(name="Idle", ats="ashby", board_id="x", status="paused")
    storage = seeded_storage(tmp_path, ws, paused)
    adapters = {"ashby": AshbyAdapter(fixture_client("ashby_wealthsimple"))}

    events = []
    summary = run_pipeline(storage, adapters, criteria, now, on_progress=events.append)

    assert summary.skipped == ["Idle (paused)"]
    assert {e.company for e in events} == {"Wealthsimple"}
    assert all(e.company_index == 1 and e.company_count == 1 for e in events)  # paused not counted
    stages = [e.stage for e in events]
    assert stages[0] == "fetching postings" and stages[-1] == "writing rows"
    assert (
        max(e.done for e in events if e.stage.startswith("filtering")) == 5
    )  # 6 postings, 0-based


def test_real_config_keeps_only_wealthsimple_data_and_engineering(tmp_path, real_criteria):
    """End to end with the real criteria.yaml: Ashby fetch -> rules -> Jobs tab.

    The fixture has 2 Data & Engineering postings plus Product, Finance and Commercial &
    Marketing ones (all real, Remote (Canada), posted in Sept 2026).
    """
    now = datetime(2026, 9, 30, tzinfo=UTC)
    ws = Company(name="Wealthsimple", ats="ashby", board_id="wealthsimple", status="active")
    storage = seeded_storage(tmp_path, ws)
    adapters = {"ashby": AshbyAdapter(fixture_client("ashby_wealthsimple_departments"))}

    result = run_pipeline(storage, adapters, real_criteria, now).results[0]

    assert result.fetched == 5
    assert result.added == 2
    assert result.rejected == {"include:department": 3}
    jobs = storage.read_records("Jobs")
    assert {j["department"] for j in jobs} == {"Data & Engineering"}
    assert {j["title"] for j in jobs} == {
        "Senior Software Developer, Post-Trade Management",
        "Senior Software Developer, AI Platform",
    }
    assert {j["state"] for j in jobs} == {"filtered"}
