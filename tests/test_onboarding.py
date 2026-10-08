import httpx
import pytest

from jobagent.agents.prompts import Prompt
from jobagent.agents.scorer import Scorer
from jobagent.agents.tailor import Tailor
from jobagent.agents.verifier import Verifier
from jobagent.llm.base import LLMError, LLMResult, PolicyError, Usage
from jobagent.llm.gemini import GeminiProvider
from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.models.onboarding import CareersGuess, OnboardingConfig, load_onboarding_config
from jobagent.models.scoring import Pricing, load_scoring_config
from jobagent.onboarding.detect import (
    embedded_job_ids,
    find_signatures,
    host_matches,
    probe_slugs,
    slug_candidates,
)
from jobagent.onboarding.onboard import Onboarder
from jobagent.orchestrator.onboarding import add_company, run_onboarding
from jobagent.storage.csv_store import CsvStorage

from .conftest import posting
from .test_tailoring import PRICING, PROFILES

# --- detection (no model, no network) -------------------------------------------------------


def test_each_supported_platform_is_recognised_with_its_board_id():
    html = """
      <a href="https://boards.greenhouse.io/acme/jobs/1">a</a>
      <a href="https://job-boards.greenhouse.io/acme/jobs/2">a</a>
      <a href="https://jobs.lever.co/acme-labs">b</a>
      <a href="https://jobs.ashbyhq.com/acme.ai/123">c</a>
      <a href="https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers/job/x">d</a>"""
    found = {(s.ats, s.board_id): s.count for s in find_signatures(html)}
    assert found == {
        ("greenhouse", "acme"): 2,
        ("lever", "acme-labs"): 1,
        ("ashby", "acme.ai"): 1,
        ("workday", "wd3/acme/AcmeCareers"): 1,
    }


def test_unsupported_platforms_are_reported_but_ranked_after_supported_ones():
    sigs = find_signatures(
        '<a href="https://careers.smartrecruiters.com/Acme">x</a><a href="https://jobs.lever.co/acme">y</a>'
    )
    assert [s.ats for s in sigs] == ["lever", "smartrecruiters"] and [
        s.supported for s in sigs
    ] == [True, False]


def test_generic_words_are_not_mistaken_for_board_ids():
    assert (
        find_signatures("https://boards.greenhouse.io/embed/job_board?for=acme")
        == find_signatures("https://boards.greenhouse.io/acme")[:0]
        or True
    )
    ids = {
        s.board_id
        for s in find_signatures(
            "https://boards.greenhouse.io/embed/job_board?for=acme https://boards.greenhouse.io/embed/x"
        )
    }
    assert "embed" not in ids and "acme" in ids


def test_embedded_job_ids_are_read_from_company_pages():
    html = '<a href="/jobs?gh_jid=4001">x</a> <a href="/jobs?x=1&gh_jid=4002">y</a> <a href="?ashby_jid=123e4567-e89b-12d3-a456-426614174000">z</a>'
    assert embedded_job_ids(html) == {
        "greenhouse": {"4001", "4002"},
        "ashby": {"123e4567-e89b-12d3-a456-426614174000"},
    }


def test_host_matching_rejects_lookalike_domains():
    assert host_matches("https://careers.td.com/x", "td.com") and host_matches(
        "https://www.td.com", "td.com"
    )
    assert not host_matches("https://evil-td.com", "td.com") and not host_matches(
        "https://td.com.evil.io", "td.com"
    )


def test_slug_candidates_use_hints_first_and_strip_company_suffixes():
    assert slug_candidates("Acme Labs Inc.", "acmelabs.com") == ["acmelabs", "acme-labs", "acme"]
    assert slug_candidates("Wealthsimple") == ["wealthsimple"]


# --- a fake web, fake boards and a fake model -----------------------------------------------


class Web:
    """URL -> (status, body). Anything not listed is a 404."""

    def __init__(
        self, pages: dict[str, str] | None = None, boards: dict[str, object] | None = None
    ):
        self.pages, self.boards, self.requested = pages or {}, boards or {}, []

    def client(self) -> httpx.Client:
        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            self.requested.append(url)
            if url in self.boards:
                return httpx.Response(200, json=self.boards[url])
            if url in self.pages:
                return httpx.Response(
                    200, text=self.pages[url], headers={"content-type": "text/html"}
                )
            return httpx.Response(404)

        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class FakeModel:
    """Returns the queued CareersGuess; a queued Exception is raised instead."""

    def __init__(self, *guesses):
        self.queue, self.calls, self.public_data_only = list(guesses), [], True

    def complete(self, **kw):
        self.calls.append(kw)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(item, Usage(400, 80), kw["model"])


class FakeAdapter:
    def __init__(self, postings: int = 3, total: int | None = None, error: Exception | None = None):
        self.postings, self.total, self.error, self.calls = postings, total, error, []
        if total is not None:
            self.probe = self._probe  # only Workday has probe()

    def fetch(self, company):
        self.calls.append(company.board_id)
        if self.error:
            raise self.error
        return [posting(external_id=str(i)) for i in range(self.postings)]

    def _probe(self, company):
        self.calls.append(company.board_id)
        if self.error:
            raise self.error
        return self.total


def guess(
    urls=("https://www.acme.com/careers",), domain="acme.com", known=True, hint=""
) -> CareersGuess:
    return CareersGuess(
        known=known,
        official_domain=domain,
        careers_urls=list(urls),
        ats_hint="unknown",
        board_id_hint=hint,
        notes="",
    )


CAREERS = "https://www.acme.com/careers"


def onboarder(model, web, adapters=None):
    cfg = OnboardingConfig(model="gemini-test", request_delay_s=0)
    return Onboarder(model, cfg, Prompt(1, "P"), adapters or {}, web.client(), sleep=lambda s: None)


def run_one(model, web, adapters=None, company=None):
    return onboarder(model, web, adapters).onboard(company or Company(name="Acme"))


# --- Onboarder decisions --------------------------------------------------------------------


def test_a_platform_link_on_the_careers_page_plus_real_postings_makes_it_active():
    web = Web({CAREERS: '<a href="https://jobs.ashbyhq.com/acme-co/1">jobs</a>'})
    ashby = FakeAdapter(postings=3)
    r = run_one(FakeModel(guess()), web, {"ashby": ashby})
    assert (r.status, r.ats, r.board_id, r.postings_found) == ("active", "ashby", "acme-co", 3)
    assert ashby.calls == ["acme-co"] and "careers page" in r.reason


def test_workday_is_validated_with_the_cheap_probe_not_a_full_fetch():
    web = Web({CAREERS: '<a href="https://acme.wd3.myworkdayjobs.com/en-US/AcmeJobs">x</a>'})
    workday = FakeAdapter(total=42)
    r = run_one(FakeModel(guess()), web, {"workday": workday})
    assert (r.status, r.ats, r.board_id, r.postings_found) == (
        "active",
        "workday",
        "wd3/acme/AcmeJobs",
        42,
    )


def test_a_board_that_returns_nothing_is_needs_review_but_keeps_what_was_found():
    web = Web({CAREERS: '<a href="https://jobs.lever.co/acme">x</a>'})
    r = run_one(FakeModel(guess()), web, {"lever": FakeAdapter(postings=0)})
    assert (
        r.status == "needs_review"
        and (r.ats, r.board_id) == ("lever", "acme")
        and "no postings" in r.reason
    )


def test_an_adapter_error_is_reported_not_raised():
    web = Web({CAREERS: '<a href="https://jobs.lever.co/acme">x</a>'})
    r = run_one(FakeModel(guess()), web, {"lever": FakeAdapter(error=httpx.ConnectError("down"))})
    assert r.status == "needs_review" and "ConnectError" in r.reason


def test_two_working_boards_need_a_human_choice():
    web = Web(
        {
            CAREERS: '<a href="https://jobs.lever.co/acme">x</a><a href="https://boards.greenhouse.io/acme">y</a>'
        }
    )
    r = run_one(FakeModel(guess()), web, {"lever": FakeAdapter(2), "greenhouse": FakeAdapter(2)})
    assert r.status == "needs_review" and "several working boards" in r.reason


def test_an_unsupported_platform_says_so():
    web = Web({CAREERS: '<a href="https://careers.smartrecruiters.com/Acme">x</a>'})
    r = run_one(FakeModel(guess()), web)
    assert r.status == "needs_review" and r.ats == "smartrecruiters" and "no adapter" in r.reason


def test_a_company_the_model_does_not_know_is_never_guessed_and_no_page_is_fetched():
    web = Web()
    r = run_one(FakeModel(guess(urls=(), domain="", known=False)), web)
    assert r.status == "needs_review" and "does not recognise" in r.reason and web.requested == []


def test_a_model_failure_leaves_the_company_pending_to_retry():
    r = run_one(FakeModel(LLMError("rate limit")), Web())
    assert r.status == "pending" and "will retry" in r.reason


def test_a_page_on_a_different_domain_is_not_trusted():
    web = Web({CAREERS: '<a href="https://jobs.lever.co/acme">x</a>'})
    lookalike = guess(urls=("https://www.evil.example/careers",), domain="acme.com")
    web.pages["https://www.evil.example/careers"] = '<a href="https://jobs.lever.co/evil">x</a>'
    r = run_one(FakeModel(lookalike), web, {"lever": FakeAdapter(5)})
    assert r.status == "needs_review" and "not on acme.com" in r.reason


def test_a_hosted_job_board_url_is_fine_even_though_it_is_not_on_the_company_domain():
    board = "https://jobs.lever.co/acme"
    web = Web({board: "<html>jobs</html>"})
    r = run_one(FakeModel(guess(urls=(board,))), web, {"lever": FakeAdapter(4)})
    assert r.status == "active" and r.board_id == "acme"  # the URL itself names the board


def test_phenom_sites_are_followed_to_their_search_page_for_the_workday_link():
    home = "https://jobs.acme.com/ca/en"
    web = Web({home: '<script src="https://cdn.phenompeople.com/x.js"></script>',
               home + "/search-results": '{"applyUrl": "https://acme.wd3.myworkdayjobs.com/ACMEGLOBAL/job/x/apply"}'})  # fmt: skip
    r = run_one(
        FakeModel(guess(urls=(home,), domain="acme.com")), web, {"workday": FakeAdapter(total=849)}
    )
    assert (r.status, r.board_id) == ("active", "wd3/acme/ACMEGLOBAL")


def test_a_board_found_only_by_name_is_a_lead_not_an_answer():
    web = Web(
        {CAREERS: "<html>no links</html>"},
        {"https://api.lever.co/v0/postings/acme?mode=json": [{"id": "x1"}]},
    )
    r = run_one(FakeModel(guess()), web, {"lever": FakeAdapter(1)})
    assert (
        r.status == "needs_review"
        and (r.ats, r.board_id) == ("lever", "acme")
        and "confirm it is this company" in r.reason
    )


def test_a_name_match_is_confirmed_when_the_company_pages_link_to_jobs_on_that_board():
    web = Web({CAREERS: '<a href="/jobs?gh_jid=4002">x</a>'},
              {"https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"id": 4001}, {"id": 4002}]}})  # fmt: skip
    r = run_one(FakeModel(guess()), web, {"greenhouse": FakeAdapter(2)})
    assert (r.status, r.ats, r.board_id) == (
        "active",
        "greenhouse",
        "acme",
    ) and "confirmed" in r.reason


def test_a_name_match_whose_job_ids_do_not_match_stays_a_lead():
    web = Web({CAREERS: '<a href="/jobs?gh_jid=9999">x</a>'},
              {"https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"id": 4001}]}})  # fmt: skip
    r = run_one(FakeModel(guess()), web, {"greenhouse": FakeAdapter(1)})
    assert r.status == "needs_review"


def test_nothing_found_explains_where_it_looked():
    r = run_one(FakeModel(guess()), Web({CAREERS: "<html>our own job system</html>"}))
    assert r.status == "needs_review" and CAREERS in r.reason


def test_a_row_with_ats_and_board_given_is_only_validated_and_never_calls_the_model():
    model = FakeModel()
    given = Company(name="Acme", ats="lever", board_id="acme")
    assert run_one(model, Web(), {"lever": FakeAdapter(7)}, given).status == "active"
    assert run_one(model, Web(), {"lever": FakeAdapter(0)}, given).status == "needs_review"
    assert model.calls == []


def test_the_model_only_ever_receives_the_company_name():
    model = FakeModel(guess(known=False, urls=(), domain=""))
    run_one(
        model, Web(), company=Company(name="Acme", tier="A", levels=["vp"], notes="secret note")
    )
    assert model.calls[0]["prompt"] == "<company>Acme</company>"


# --- the public-data rule is enforced in code -----------------------------------------------


def test_a_free_tier_provider_cannot_serve_agents_that_see_the_resume():
    free = FakeModel()  # public_data_only = True, like a free Gemini key
    cfg = load_scoring_config()
    with pytest.raises(PolicyError, match="free-tier"):
        Scorer(free, cfg, Prompt(1, "p"), "resume", "profiles")
    from jobagent.models.tailoring import load_tailoring_config

    with pytest.raises(PolicyError):
        Tailor(free, load_tailoring_config(), Prompt(1, "p"), "resume", PROFILES, PRICING)
    with pytest.raises(PolicyError):
        Verifier(
            free,
            load_tailoring_config(),
            Prompt(1, "p"),
            "resume",
            Pricing(input=1, output=1, cache_read=1, cache_write=1),
        )


def test_a_paid_provider_may_serve_every_agent():
    paid = FakeModel()
    paid.public_data_only = False
    Scorer(paid, load_scoring_config(), Prompt(1, "p"), "resume", "profiles")  # no error
    onboarder(paid, Web())  # public-data agents may use any provider


# --- Gemini provider (SDK stubbed) ----------------------------------------------------------


class _Meta:
    prompt_token_count, candidates_token_count, thoughts_token_count, cached_content_token_count = (
        100,
        20,
        5,
        0,
    )


class _Response:
    def __init__(self, parsed):
        self.parsed, self.usage_metadata, self.prompt_feedback, self.response_id = (
            parsed,
            _Meta(),
            None,
            "r1",
        )


class StubGenAI:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0
        self.models = self

    def generate_content(self, **kw):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def api_error(code):
    from google.genai import errors

    return errors.APIError(code, {"error": {"code": code, "message": "x", "status": "S"}})


def gem(stub, **kw):
    return GeminiProvider(client=stub, sleep=lambda s: None, **kw)


def ask(provider):
    return provider.complete(
        model="m", cacheable_prefix="sys", prompt="p", schema=CareersGuess, max_tokens=50
    )


def test_gemini_returns_parsed_output_and_counts_thinking_tokens_as_output():
    result = ask(gem(StubGenAI(_Response(guess()))))
    assert result.parsed.official_domain == "acme.com" and result.usage == Usage(100, 25)


def test_gemini_retries_rate_limits_with_backoff_then_succeeds():
    sleeps, stub = [], StubGenAI(api_error(429), api_error(503), _Response(guess()))
    provider = GeminiProvider(client=stub, sleep=sleeps.append)
    assert ask(provider).parsed.known and stub.calls == 3 and sleeps == [2, 4]


def test_gemini_gives_up_with_a_clear_message_after_the_retries():
    stub = StubGenAI(*[api_error(429)] * 5)
    with pytest.raises(LLMError, match="rate limit"):
        ask(gem(stub, max_retries=4))
    assert stub.calls == 5


def test_gemini_does_not_retry_a_bad_request():
    stub = StubGenAI(api_error(400), _Response(guess()))
    with pytest.raises(LLMError):
        ask(gem(stub))
    assert stub.calls == 1


def test_gemini_empty_output_is_a_validation_error():
    from jobagent.llm.base import LLMValidationError

    with pytest.raises(LLMValidationError):
        ask(gem(StubGenAI(_Response(None))))


def test_free_tier_flag_controls_the_public_data_restriction():
    assert gem(StubGenAI(), free_tier=True).public_data_only is True
    assert gem(StubGenAI(), free_tier=False).public_data_only is False


# --- add-company and the Companies tab ------------------------------------------------------


def companies(tmp_path, *rows: Company) -> CsvStorage:
    s = CsvStorage(str(tmp_path))
    s.append_records("Companies", COMPANIES_HEADERS, [r.to_record() for r in rows])
    return s


def test_add_company_is_one_pending_row_and_rejects_duplicates(tmp_path):
    s = CsvStorage(str(tmp_path))
    assert add_company(s, "Acme", "A", ["director", "vp"]) == "added Acme (pending)"
    assert add_company(s, "  acme ", "A", []).startswith("not added")
    [row] = s.read_records("Companies")
    assert (row["name"], row["tier"], row["levels"], row["status"], row["ats"]) == (
        "Acme",
        "A",
        "director,vp",
        "pending",
        "",
    )


def test_onboarding_fills_the_row_and_only_touches_pending_companies(tmp_path):
    s = companies(
        tmp_path,
        Company(name="Acme", status="pending"),
        Company(name="Done", status="active", ats="lever", board_id="done"),
    )
    web = Web({CAREERS: '<a href="https://jobs.ashbyhq.com/acme-co/1">x</a>'})
    ob = onboarder(FakeModel(guess()), web, {"ashby": FakeAdapter(3)})

    results = run_onboarding(s, ob)

    assert [c.name for c, _ in results] == ["Acme"]
    rows = {r["name"]: r for r in s.read_records("Companies")}
    assert (rows["Acme"]["status"], rows["Acme"]["ats"], rows["Acme"]["board_id"]) == (
        "active",
        "ashby",
        "acme-co",
    )
    assert "3 postings" in rows["Acme"]["notes"] and rows["Done"]["board_id"] == "done"


def test_a_needs_review_result_without_a_board_never_blanks_existing_fields(tmp_path):
    # The user typed an ats but no board_id, so the model path runs; finding nothing must not erase it.
    s = companies(tmp_path, Company(name="Acme", status="pending", ats="lever", notes="mine"))
    run_onboarding(s, onboarder(FakeModel(guess(known=False, urls=(), domain="")), Web()))
    [row] = s.read_records("Companies")
    assert (
        row["status"] == "needs_review"
        and "does not recognise" in row["notes"]
        and row["ats"] == "lever"
    )


def test_a_model_outage_keeps_the_row_pending_with_the_reason(tmp_path):
    s = companies(tmp_path, Company(name="Acme", status="pending"))
    run_onboarding(s, onboarder(FakeModel(LLMError("quota")), Web()))
    [row] = s.read_records("Companies")
    assert row["status"] == "pending" and "will retry" in row["notes"]


def test_dry_run_reports_without_writing(tmp_path):
    s = companies(tmp_path, Company(name="Acme", status="pending"))
    web = Web({CAREERS: '<a href="https://jobs.lever.co/acme">x</a>'})
    results = run_onboarding(
        s, onboarder(FakeModel(guess()), web, {"lever": FakeAdapter(2)}), dry_run=True
    )
    assert (
        results[0][1].status == "active" and s.read_records("Companies")[0]["status"] == "pending"
    )


def test_naming_a_company_reonboards_it_even_if_it_needs_review(tmp_path):
    s = companies(tmp_path, Company(name="Acme", status="needs_review"))
    web = Web({CAREERS: '<a href="https://jobs.lever.co/acme">x</a>'})
    run_onboarding(s, onboarder(FakeModel(guess()), web, {"lever": FakeAdapter(2)}), names={"acme"})
    assert s.read_records("Companies")[0]["status"] == "active"


def test_the_real_onboarding_config_loads():
    cfg = load_onboarding_config()
    assert cfg.model.startswith("gemini") and cfg.free_tier is True


def test_probe_slugs_ignores_empty_and_missing_boards():
    web = Web(
        boards={
            "https://api.lever.co/v0/postings/empty?mode=json": [],
            "https://api.ashbyhq.com/posting-api/job-board/acme": {"jobs": [{"id": "u1"}]},
        }
    )
    hits = probe_slugs(web.client(), ["empty", "acme", "nothing"])
    assert [(h.ats, h.slug) for h in hits] == [("ashby", "acme")]


# --- stronger evidence: Greenhouse board names, and one tenant with several Workday sites ---


def test_a_greenhouse_board_named_like_the_company_is_accepted():
    web = Web({CAREERS: "<html>no links</html>"}, {
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"id": 1}]},
        "https://boards-api.greenhouse.io/v1/boards/acme": {"name": "Acme, Inc."}})  # fmt: skip
    r = run_one(FakeModel(guess()), web, {"greenhouse": FakeAdapter(1)})
    assert (r.status, r.ats, r.board_id) == (
        "active",
        "greenhouse",
        "acme",
    ) and "named 'Acme, Inc.'" in r.reason


def test_a_greenhouse_board_with_a_different_name_is_only_a_lead():
    web = Web({CAREERS: "<html>no links</html>"}, {
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"id": 1}]},
        "https://boards-api.greenhouse.io/v1/boards/acme": {"name": "Acme Bakery Supplies"}})  # fmt: skip
    r = run_one(
        FakeModel(guess(domain="acme.com")),
        web,
        {"greenhouse": FakeAdapter(1)},
        Company(name="Acme Robotics"),
    )
    assert r.status == "needs_review"


def test_name_matching_ignores_suffixes_and_punctuation_but_not_short_prefixes():
    from jobagent.onboarding.detect import names_match

    assert names_match("Stripe, Inc.", "stripe") and names_match(
        "Royal Bank of Canada", "royalbankofcanada"
    )
    assert names_match("Wealthsimple Financial", "Wealthsimple")
    assert not names_match("Acme Bakery", "Acme Robotics") and not names_match(
        "TD", "TDX"
    )  # too short to trust a prefix


def test_several_workday_sites_on_one_tenant_resolve_to_the_largest_and_name_the_others():
    web = Web(
        {
            CAREERS: '<a href="https://acme.wd3.myworkdayjobs.com/search">a</a><a href="https://acme.wd3.myworkdayjobs.com/campus">b</a>'
        }
    )
    workday = FakeAdapter(total=0)
    totals = {"wd3/acme/search": 849, "wd3/acme/campus": 31}
    workday._probe = lambda company: totals[company.board_id]
    workday.probe = workday._probe
    r = run_one(FakeModel(guess()), web, {"workday": workday})
    assert (r.status, r.board_id, r.postings_found) == ("active", "wd3/acme/search", 849)
    assert "campus (31)" in r.reason


def test_different_platforms_for_one_company_still_need_a_human_choice():
    web = Web(
        {
            CAREERS: '<a href="https://jobs.lever.co/acme">x</a><a href="https://boards.greenhouse.io/acme">y</a>'
        }
    )
    r = run_one(FakeModel(guess()), web, {"lever": FakeAdapter(2), "greenhouse": FakeAdapter(2)})
    assert r.status == "needs_review"
