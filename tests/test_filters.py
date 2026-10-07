from datetime import UTC, datetime

from jobagent.orchestrator.filters import apply_hard_filters

from .conftest import posting


def check(p, criteria, now):
    return apply_hard_filters(p, criteria, now)


def test_toronto_director_passes(criteria, now):
    assert check(posting(), criteria, now).passed


def test_gta_suburb_and_ontario_pass(criteria, now):
    assert check(posting(locations=["Mississauga"]), criteria, now).passed
    assert check(posting(locations=["Ottawa, Ontario"]), criteria, now).passed


def test_us_onsite_rejected(criteria, now):
    r = check(posting(locations=["New York, NY"]), criteria, now)
    assert not r.passed and r.reason.startswith("location")


def test_remote_canada_passes_remote_us_rejected(criteria, now):
    assert check(posting(locations=["Remote (Canada)"], remote=True), criteria, now).passed
    r = check(posting(locations=["Remote - US"], remote=True), criteria, now)
    assert not r.passed


def test_bare_remote_kept_and_flagged(criteria, now):
    r = check(posting(locations=["Remote"], remote=True), criteria, now)
    assert r.passed and "remote_country_unknown" in r.flags


def test_missing_location_kept_and_flagged(criteria, now):
    r = check(posting(locations=[]), criteria, now)
    assert r.passed and "location_unknown" in r.flags


def test_any_secondary_location_can_match(criteria, now):
    assert check(posting(locations=["Chicago, IL", "Toronto, ON"]), criteria, now).passed


def test_inflated_titles_rejected(criteria, now):
    for title in (
        "Assistant Vice President, Lead Developer",
        "Associate Vice-President, Software Engineering",
        "AVP - Platform Engineering",
    ):
        r = check(posting(title=title), criteria, now)
        assert not r.passed and r.reason.startswith("title"), title


def test_real_vp_title_not_excluded_by_regex(criteria, now):
    assert check(posting(title="Vice President, Engineering"), criteria, now).passed


def test_old_posting_rejected(criteria, now):
    old = posting(posted_at=datetime(2026, 8, 1, tzinfo=UTC))
    r = check(old, criteria, now)
    assert not r.passed and r.reason.startswith("age")


def test_boundary_29_days_passes(criteria, now):
    assert check(posting(posted_at=datetime(2026, 9, 7, tzinfo=UTC)), criteria, now).passed


def test_undated_kept_and_flagged(criteria, now):
    r = check(posting(posted_at=None), criteria, now)
    assert r.passed and "date_unknown" in r.flags
