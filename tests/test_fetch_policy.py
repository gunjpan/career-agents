from datetime import UTC, datetime, timedelta

import pytest

from jobagent.adapters.ats.ashby import AshbyAdapter
from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.models.fetch_policy import FetchPolicy, load_fetch_policy
from jobagent.orchestrator.pipeline import run_pipeline
from jobagent.storage.csv_store import CsvStorage

from .conftest import fixture_client, posting

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
POLICY = FetchPolicy(default_min_hours=6, min_hours={"workday": 20})


def hours_ago(h: float) -> datetime:
    return NOW - timedelta(hours=h)


# --- the policy itself ------------------------------------------------------------------------


def test_cooldown_is_per_platform_with_a_default():
    assert POLICY.cooldown("ashby") == timedelta(hours=6) and POLICY.cooldown(
        "workday"
    ) == timedelta(hours=20)


def test_never_fetched_or_cooled_down_means_no_wait():
    assert POLICY.wait_remaining("ashby", None, NOW) == timedelta(0)
    assert POLICY.wait_remaining("ashby", hours_ago(6), NOW) == timedelta(0)  # exactly at the limit
    assert POLICY.wait_remaining("ashby", hours_ago(2), NOW) == timedelta(hours=4)
    assert POLICY.wait_remaining("workday", hours_ago(2), NOW) == timedelta(hours=18)


def test_a_timestamp_without_a_timezone_is_read_as_utc():
    naive = datetime(2026, 9, 28, 10, 0)  # noqa: DTZ001 - deliberately timezone-less
    assert POLICY.wait_remaining("ashby", naive, NOW) == timedelta(hours=4)


def test_the_real_policy_is_gentler_on_workday():
    real = load_fetch_policy()
    assert real.cooldown("workday") > real.cooldown("ashby") >= timedelta(hours=1)
    assert real.cooldown("workday") < timedelta(
        hours=24
    )  # a daily cron must never be skipped by drift


# --- the pipeline honours it ------------------------------------------------------------------


class CountingAdapter:
    """Wraps a real (fixture-backed) adapter and counts how often it is asked to fetch."""

    def __init__(self, inner=None, error: Exception | None = None):
        self.inner, self.error, self.fetches = inner, error, 0

    def fetch(self, company):
        self.fetches += 1
        if self.error:
            raise self.error
        return self.inner.fetch(company) if self.inner else [posting(external_id="1")]


def companies(tmp_path, *rows: Company) -> CsvStorage:
    s = CsvStorage(str(tmp_path))
    s.append_records("Companies", COMPANIES_HEADERS, [r.to_record() for r in rows])
    return s


def co(name, ats="ashby", last=None) -> Company:
    return Company(name=name, ats=ats, board_id=name.lower(), status="active", last_fetched=last)


def row(storage, name):
    return next(r for r in storage.read_records("Companies") if r["name"] == name)


def go(storage, adapters, criteria, **kw):
    return run_pipeline(storage, adapters, criteria, NOW, policy=POLICY, **kw)


def test_a_recently_fetched_company_is_skipped_and_a_new_one_is_fetched(tmp_path, criteria):
    storage = companies(tmp_path, co("Recent", last=hours_ago(2)), co("Brandnew"))
    adapter = CountingAdapter()
    summary = go(storage, {"ashby": adapter}, criteria)

    assert adapter.fetches == 1  # only the new company
    by = {r.company: r for r in summary.results}
    assert (
        "fetched 2.0h ago; next in 4.0h" in by["Recent"].cooldown and by["Brandnew"].cooldown == ""
    )
    assert row(storage, "Recent")["last_fetched"] == hours_ago(2).isoformat(
        timespec="seconds"
    )  # untouched


def test_the_clock_is_stamped_so_an_immediate_rerun_fetches_nothing(tmp_path, criteria):
    storage = companies(tmp_path, co("Acme"))
    adapter = CountingAdapter()
    go(storage, {"ashby": adapter}, criteria)
    assert adapter.fetches == 1 and row(storage, "Acme")["last_fetched"] == NOW.isoformat(
        timespec="seconds"
    )

    go(storage, {"ashby": adapter}, criteria)
    assert adapter.fetches == 1  # second run inside the cooldown: no request at all


def test_after_the_cooldown_the_company_is_fetched_again(tmp_path, criteria):
    storage = companies(tmp_path, co("Acme"))
    adapter = CountingAdapter()
    go(storage, {"ashby": adapter}, criteria)
    run_pipeline(
        storage, {"ashby": adapter}, criteria, NOW + timedelta(hours=6, minutes=1), policy=POLICY
    )
    assert adapter.fetches == 2


def test_workday_waits_longer_than_the_simple_boards(tmp_path, criteria):
    storage = companies(
        tmp_path, co("Simple", "ashby", hours_ago(7)), co("Big", "workday", hours_ago(7))
    )
    ashby, workday = CountingAdapter(), CountingAdapter()
    go(storage, {"ashby": ashby, "workday": workday}, criteria)
    assert (ashby.fetches, workday.fetches) == (1, 0)  # 7h is past 6h but not 20h


def test_force_ignores_the_cooldown(tmp_path, criteria):
    storage = companies(tmp_path, co("Recent", last=hours_ago(1)))
    adapter = CountingAdapter()
    go(storage, {"ashby": adapter}, criteria, force=True)
    assert adapter.fetches == 1


def test_only_limits_the_run_to_the_named_companies(tmp_path, criteria):
    storage = companies(tmp_path, co("One"), co("Two"))
    adapter = CountingAdapter()
    summary = go(storage, {"ashby": adapter}, criteria, only={"two"})
    assert adapter.fetches == 1 and [r.company for r in summary.results] == ["Two"]
    assert row(storage, "One")["last_fetched"] == ""


def test_a_failing_board_is_stamped_too_so_reruns_do_not_hammer_it(tmp_path, criteria):
    storage = companies(tmp_path, co("Flaky"))
    broken = CountingAdapter(error=RuntimeError("boom"))
    first = go(storage, {"ashby": broken}, criteria)
    assert "boom" in first.results[0].error and broken.fetches == 1

    go(storage, {"ashby": broken}, criteria)
    assert broken.fetches == 1


def test_without_a_policy_nothing_is_skipped(tmp_path, criteria):
    storage = companies(tmp_path, co("Recent", last=hours_ago(0.1)))
    adapter = CountingAdapter()
    run_pipeline(storage, {"ashby": adapter}, criteria, NOW)
    assert adapter.fetches == 1


def test_a_companies_tab_from_before_the_column_existed_gains_it(tmp_path, criteria):
    storage = CsvStorage(str(tmp_path))
    storage.append_records(
        "Companies", COMPANIES_HEADERS[:-1], [co("Old").to_record() | {}]
    )  # no last_fetched column
    adapter = CountingAdapter(AshbyAdapter(fixture_client("ashby_wealthsimple")))
    go(storage, {"ashby": adapter}, criteria)
    assert adapter.fetches == 1 and row(storage, "Old")["last_fetched"] == NOW.isoformat(
        timespec="seconds"
    )


@pytest.mark.parametrize("hours", [0, 0.5, 5.99])
def test_anything_inside_the_cooldown_is_skipped(tmp_path, criteria, hours):
    storage = companies(tmp_path, co("Acme", last=hours_ago(hours)))
    adapter = CountingAdapter()
    go(storage, {"ashby": adapter}, criteria)
    assert adapter.fetches == 0
