from jobagent.adapters.ats.ashby import AshbyAdapter
from jobagent.adapters.ats.base import html_to_text
from jobagent.adapters.ats.greenhouse import GreenhouseAdapter
from jobagent.adapters.ats.lever import LeverAdapter

from .conftest import company, fixture_client


def test_ashby_maps_fields():
    jobs = AshbyAdapter(fixture_client("ashby_wealthsimple")).fetch(company("Wealthsimple"))
    assert len(jobs) == 6
    first = jobs[0]
    assert first.company == "Wealthsimple"
    assert first.title == "Lead, Workplace Operations"
    assert first.locations == ["Toronto Headquarters"]
    assert first.posted_at is not None and first.posted_at.year == 2026
    assert first.url.startswith("https://jobs.ashbyhq.com/wealthsimple/")
    assert jobs[2].remote is True and jobs[2].workplace_type == "remote"


def test_greenhouse_maps_fields_and_cleans_html():
    jobs = GreenhouseAdapter(fixture_client("greenhouse_stripe")).fetch(
        company("Stripe", "greenhouse", "stripe")
    )
    assert len(jobs) == 3
    j = jobs[0]
    assert j.external_id.isdigit()
    assert j.posted_at is not None  # from first_published, not updated_at
    assert "<" not in j.description and "&lt;" not in j.description


def test_lever_maps_epoch_millis_and_workplace_type():
    jobs = LeverAdapter(fixture_client("lever_palantir")).fetch(
        company("Palantir", "lever", "palantir")
    )
    assert len(jobs) == 3
    j = jobs[0]
    assert j.posted_at is not None and j.posted_at.tzinfo is not None
    assert j.workplace_type in {"remote", "hybrid", "on-site", "onsite", None}
    assert j.locations


def test_html_to_text_handles_escaped_html():
    assert html_to_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;li&gt;One&lt;/li&gt;") == (
        "Hello & welcome\nOne"
    )


def test_department_captured_where_the_ats_provides_it():
    ashby = AshbyAdapter(fixture_client("ashby_wealthsimple")).fetch(company("Wealthsimple"))
    assert any(j.department for j in ashby)
    gh = GreenhouseAdapter(fixture_client("greenhouse_stripe")).fetch(
        company("Stripe", "greenhouse", "stripe")
    )
    assert any(j.department for j in gh)
