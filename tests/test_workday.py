import json
from datetime import UTC, datetime

import httpx
import pytest

from jobagent.adapters.ats.base import Enrichable
from jobagent.adapters.ats.workday import (
    WorkdayAdapter,
    find_country_facet,
    find_family_values,
    parse_board_id,
    relative_posted_at,
)
from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.models.criteria import DiscoveryCriteria
from jobagent.orchestrator.pipeline import run_pipeline
from jobagent.storage.csv_store import CsvStorage

from .conftest import FIXTURES

NOW = datetime(2026, 10, 6, tzinfo=UTC)
CA_ID = "a30a87ed25634629aa6c3958aa2b91ea"
RBC = Company(name="RBC", ats="workday", board_id="wd3/rbc/RBCGLOBAL1", status="active")
DATA = json.loads((FIXTURES / "workday_rbc.json").read_text())


FAMILY_FACET = {
    "facetParameter": "Category",
    "values": [
        {"descriptor": "Technology", "id": "tech1", "count": 21},
        {"descriptor": "Sales and Advisory", "id": "sales1", "count": 31},
        {"descriptor": "Analytics and Data Management", "id": "data1", "count": 5},
    ],
}


@pytest.fixture
def criteria(criteria):
    """Pipeline mechanics, not personal filter choices: keep locations and age, drop the rules."""
    return criteria.model_copy(update={"rules": []})


def make_adapter(
    requests: list, *, terms=("director",), country="Canada", fail_detail=False, families=()
):
    """Workday stand-in: replays the recorded responses, including the real paging quirk
    (total is reported only on the first page) and a facet probe on limit=1."""
    page = DATA["page"]

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "GET":
            if fail_detail:
                return httpx.Response(500)
            return httpx.Response(200, json=DATA["detail"])
        body = json.loads(req.content)
        if body["limit"] == 1:
            probe = {**DATA["probe"], "facets": [*DATA["probe"]["facets"], FAMILY_FACET]}
            return httpx.Response(200, json=probe)
        if body["offset"] == 0:
            return httpx.Response(200, json=page)
        return httpx.Response(200, json={**page, "total": 0, "jobPostings": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    discovery = DiscoveryCriteria(
        search_terms=list(terms), country=country, job_family_patterns=list(families)
    )
    return WorkdayAdapter(client, discovery, clock=lambda: NOW, sleep=lambda s: None)


def test_parse_board_id():
    assert parse_board_id("wd3/rbc/RBCGLOBAL1") == ("wd3", "rbc", "RBCGLOBAL1")
    with pytest.raises(ValueError):
        parse_board_id("rbc")


def test_relative_dates():
    assert relative_posted_at("Posted Today", NOW) == NOW
    assert (NOW - relative_posted_at("Posted Yesterday", NOW)).days == 1
    assert (NOW - relative_posted_at("Posted 27 Days Ago", NOW)).days == 27
    assert (NOW - relative_posted_at("Posted 30+ Days Ago", NOW)).days == 31
    assert relative_posted_at("something else", NOW) is None


def test_country_facet_found_flat_and_nested():
    flat = [{"facetParameter": "Country", "values": [{"descriptor": "Canada", "id": "c1"}]}]
    nested = [
        {
            "facetParameter": "locationMainGroup",
            "values": [
                {
                    "facetParameter": "locationCountry",
                    "values": [{"descriptor": "Canada", "id": "c2"}],
                }
            ],
        }
    ]
    assert find_country_facet(flat, "canada") == ("Country", "c1")
    assert find_country_facet(nested, "Canada") == ("locationCountry", "c2")
    assert find_country_facet(flat, "France") is None


def test_fetch_applies_country_facet_and_maps_list_fields():
    requests: list = []
    postings = make_adapter(requests).fetch(RBC)

    assert len(postings) == 5
    searches = [json.loads(r.content) for r in requests if r.method == "POST"]
    assert searches[0]["limit"] == 1  # facet probe
    assert searches[1]["appliedFacets"] == {"Country": [CA_ID]}
    assert searches[1]["searchText"] == "director"

    p = postings[0]
    assert p.company == "RBC" and p.ats == "workday"
    assert p.locations == ["TORONTO, Ontario, Canada"]
    assert p.url.startswith("https://rbc.wd3.myworkdayjobs.com/RBCGLOBAL1/job/")
    assert p.external_id.startswith("R-")
    assert (NOW - p.posted_at).days == 27 and p.description == ""


def test_fetch_stops_after_first_page_when_total_is_reached():
    requests: list = []
    adapter = make_adapter(requests, country=None)
    adapter.fetch(RBC)
    # total=85 on page 1 in the fixture, so a second page is requested; the replay returns
    # an empty page (total 0) and the loop must stop rather than keep paging.
    offsets = [json.loads(r.content)["offset"] for r in requests]
    assert offsets == [0, 20]


def test_find_family_values_matches_patterns_and_handles_missing_facet():
    facets = [FAMILY_FACET]
    assert find_family_values(facets, ["technology", r"\bdata\b"]) == (
        "Category",
        [("Technology", "tech1"), ("Analytics and Data Management", "data1")],
    )
    assert find_family_values(facets, ["nomatch"]) == ("Category", [])
    assert find_family_values([{"facetParameter": "Country", "values": []}], ["x"]) is None


def test_family_filter_searches_each_matching_family_and_tags_department():
    requests: list = []
    adapter = make_adapter(requests, families=["technology", r"\bdata\b"])
    postings = adapter.fetch(RBC)

    searches = [json.loads(r.content) for r in requests if r.method == "POST"][1:]  # skip probe
    sent = {tuple(s["appliedFacets"]["Category"]) for s in searches}
    assert sent == {("tech1",), ("data1",)}  # Sales and Advisory is never searched
    assert all(s["appliedFacets"]["Country"] == [CA_ID] for s in searches)
    assert {p.department for p in postings} == {"Technology"}  # first family to return it wins


def test_no_family_facet_on_tenant_falls_back_to_unfiltered(monkeypatch):
    monkeypatch.setitem(
        FAMILY_FACET, "facetParameter", "Unrelated"
    )  # tenant exposes no recognised family facet
    postings = make_adapter([], families=["technology"]).fetch(RBC)
    assert len(postings) == 5 and {p.department for p in postings} == {""}


def test_stub_entries_without_path_or_title_are_skipped():
    adapter = make_adapter([])
    stub = {"bulletFields": ["R_1511263"]}  # seen in real TD responses
    DATA["page"]["jobPostings"].append(stub)
    try:
        assert len(adapter.fetch(RBC)) == 5
    finally:
        DATA["page"]["jobPostings"].remove(stub)


def test_unexpected_fetch_error_is_isolated_to_that_company(tmp_path, criteria):
    class Exploding:
        name = "workday"

        def fetch(self, company):
            raise KeyError("surprise")

    ok = Company(name="Fine", ats="workday", board_id="wd3/rbc/RBCGLOBAL1", status="active")
    storage = CsvStorage(str(tmp_path))
    storage.append_records("Companies", COMPANIES_HEADERS, [RBC.to_record(), ok.to_record()])
    summary = run_pipeline(storage, {"workday": Exploding()}, criteria, NOW)
    assert [r.error for r in summary.results] == ["fetch failed: KeyError: 'surprise'"] * 2


def test_multiple_terms_merge_without_duplicates():
    postings = make_adapter([], terms=("director", "vice president")).fetch(RBC)
    assert len(postings) == 5  # same 5 postings returned for both terms


def test_enrich_fills_exact_date_locations_and_description():
    adapter = make_adapter([])
    listed = adapter.fetch(RBC)[0]
    full = adapter.enrich(listed)

    assert full.posted_at == datetime(2026, 9, 9, tzinfo=UTC)
    assert full.locations[0] == "TORONTO, Ontario, Canada"
    assert len(full.description) > 100 and "<" not in full.description
    assert full.title == listed.title and full.external_id == listed.external_id


def test_workday_adapter_is_enrichable():
    assert isinstance(make_adapter([]), Enrichable)


def seeded(tmp_path) -> CsvStorage:
    storage = CsvStorage(str(tmp_path))
    storage.append_records("Companies", COMPANIES_HEADERS, [RBC.to_record()])
    return storage


def test_pipeline_enriches_only_new_postings(tmp_path, criteria):
    storage = seeded(tmp_path)
    requests: list = []
    adapters = {"workday": make_adapter(requests)}

    first = run_pipeline(storage, adapters, criteria, NOW).results[0]
    assert first.added == 5 and first.enrich_failed == 0
    jobs = storage.read_records("Jobs")
    assert {j["state"] for j in jobs} == {"filtered"} and all(j["description"] for j in jobs)
    details_first = sum(r.method == "GET" for r in requests)
    assert details_first == 5

    second = run_pipeline(storage, adapters, criteria, NOW).results[0]
    assert second.added == 0 and second.duplicates == 5
    assert sum(r.method == "GET" for r in requests) == details_first  # no re-fetched details


def test_failed_detail_fetch_stores_nothing_and_retries_next_run(tmp_path, criteria):
    storage = seeded(tmp_path)
    broken = run_pipeline(storage, {"workday": make_adapter([], fail_detail=True)}, criteria, NOW)
    assert broken.results[0].enrich_failed == 5 and broken.results[0].added == 0
    assert storage.read_records("Jobs") == []

    healthy = run_pipeline(storage, {"workday": make_adapter([])}, criteria, NOW)
    assert healthy.results[0].added == 5
