from datetime import UTC, datetime, timedelta

import httpx
import yaml
from pydantic import SecretStr

from jobagent.agents.context import load_master_resume
from jobagent.agents.prompts import Prompt
from jobagent.agents.scorer import Scorer
from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.models.fetch_policy import FetchPolicy
from jobagent.models.scoring import load_scoring_config
from jobagent.orchestrator.daily import DailySummary, run_daily
from jobagent.settings import Settings
from jobagent.storage.csv_store import CsvStorage

from .conftest import posting
from .test_onboarding import FakeModel, Web, guess, onboarder
from .test_scoring import FakeProvider, output

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
POLICY = FetchPolicy(default_min_hours=6, min_hours={"workday": 20})
SECRET_NAME, SECRET_TITLE = "Zephyr Dynamics", "Director, Secret Skunkworks"


class Board:
    """A fake job board adapter: returns canned postings or raises."""

    def __init__(self, titles=(), error: Exception | None = None):
        self.titles, self.error, self.fetches = list(titles), error, 0

    def fetch(self, company):
        self.fetches += 1
        if self.error:
            raise self.error
        return [
            posting(company=company.name, title=t, external_id=f"{company.name}-{i}")
            for i, t in enumerate(self.titles)
        ]


def setup(tmp_path, *companies: Company) -> CsvStorage:
    s = CsvStorage(str(tmp_path))
    s.append_records("Companies", COMPANIES_HEADERS, [c.to_record() for c in companies])
    return s


def active(name=SECRET_NAME, ats="ashby", last=None) -> Company:
    return Company(
        name=name,
        ats=ats,
        board_id=name.lower().replace(" ", ""),
        status="active",
        last_fetched=last,
    )


def day(storage, criteria, adapters, **kw) -> DailySummary:
    return run_daily(
        storage,
        adapters,
        criteria,
        POLICY,
        now=NOW,
        clock=lambda: NOW + timedelta(seconds=30),
        **kw,
    )


def scorer_for(provider):
    return Scorer(
        provider, load_scoring_config(), Prompt(2, "P"), "resume", "profiles"
    ), load_scoring_config()


def runs(storage):
    return {r["command"]: r for r in storage.read_records("Runs")}


# --- the flow -------------------------------------------------------------------------------


def test_fetch_filter_score_in_one_run_and_both_stages_are_logged(tmp_path, criteria):
    storage = setup(tmp_path, active())
    scorer, cfg = scorer_for(FakeProvider([output(), output(real_level="manager", fit_score=40)]))
    summary = day(storage, criteria, {"ashby": Board(["Director, Engineering", "Senior Manager, Data"])}, scorer=scorer, scoring_config=cfg, prompt_version=2)  # fmt: skip

    jobs = storage.read_records("Jobs")
    assert len(jobs) == 2 and {j["state"] for j in jobs} <= {"scored", "shortlisted"}
    assert summary.scoring.scored == 2 and summary.fetch.results[0].added == 2
    log = runs(storage)
    assert (
        log["run"]["items"] == "2"
        and log["score"]["items"] == "2"
        and log["score"]["prompt_version"] == "2"
    )
    assert float(log["score"]["cost_usd"]) > 0 and log["run"]["cost_usd"] == "0.000000"


def test_without_a_claude_key_it_still_fetches_and_says_scoring_was_skipped(tmp_path, criteria):
    storage = setup(tmp_path, active())
    summary = day(storage, criteria, {"ashby": Board(["Director, Engineering"])})
    assert summary.scoring is None and "skipped" in summary.to_markdown()
    assert [j["state"] for j in storage.read_records("Jobs")] == [
        "filtered"
    ] and "score" not in runs(storage)


def test_a_company_inside_its_cooldown_is_not_fetched(tmp_path, criteria):
    storage = setup(tmp_path, active(last=NOW - timedelta(hours=1)))
    board = Board(["Director, Engineering"])
    summary = day(storage, criteria, {"ashby": board})
    assert board.fetches == 0 and "1 skipped (cooldown)" in summary.to_markdown()


def test_pending_companies_are_onboarded_before_the_fetch(tmp_path, criteria):
    storage = setup(tmp_path, Company(name="Newco", status="pending"))
    web = Web({"https://www.acme.com/careers": '<a href="https://jobs.lever.co/newco">x</a>'})
    from .test_onboarding import FakeAdapter

    board = Board(["Director, Engineering"])
    ob = onboarder(FakeModel(guess()), web, {"lever": FakeAdapter(3)})
    summary = day(storage, criteria, {"lever": board}, onboarder=ob)
    assert summary.onboarded == {"active": 1} and board.fetches == 1  # active in the same run
    assert "Onboarding:** 1 active" in summary.to_markdown()


# --- exit code: only a total failure should email you -----------------------------------------


def test_everything_failing_is_a_failed_run(tmp_path, criteria):
    storage = setup(tmp_path, active("One"), active("Two"))
    summary = day(storage, criteria, {"ashby": Board(error=httpx.ConnectError("down"))})
    assert summary.exit_code == 1 and summary.errors == {"ConnectError": 2}


def test_one_failure_among_successes_is_not_a_failed_run(tmp_path, criteria):
    storage = setup(tmp_path, active("One", "ashby"), active("Two", "lever"))
    summary = day(
        storage,
        criteria,
        {"ashby": Board(["Director, Engineering"]), "lever": Board(error=httpx.ConnectError("x"))},
    )
    assert summary.exit_code == 0 and summary.errors == {"ConnectError": 1}


def test_nothing_to_do_is_a_success(tmp_path, criteria):
    assert (
        day(
            setup(tmp_path, active(last=NOW - timedelta(hours=1))), criteria, {"ashby": Board()}
        ).exit_code
        == 0
    )


# --- the public log never names your targets -----------------------------------------------


def test_the_summary_has_counts_and_costs_but_no_company_names_or_titles(tmp_path, criteria):
    storage = setup(tmp_path, active(), active("Hidden Corp", "lever"))
    scorer, cfg = scorer_for(FakeProvider())
    boards = {
        "ashby": Board([SECRET_TITLE]),
        "lever": Board(error=RuntimeError(f"cannot reach {SECRET_NAME}")),
    }
    summary = day(storage, criteria, boards, scorer=scorer, scoring_config=cfg)

    text = summary.to_markdown()
    for private in (SECRET_NAME, "Hidden Corp", SECRET_TITLE, "Skunkworks", "zephyr"):
        assert private.lower() not in text.lower(), private
    assert "1 fetched" in text and "$" in text and "RuntimeError" in text  # still informative


def test_rule_names_that_contain_company_names_are_merged_into_a_family(tmp_path, criteria):
    from jobagent.models.criteria import Rule

    named_after_a_company = Rule(
        name="zephyr-no-product-roles", field="title", match="Product", action="exclude"
    )
    crit = criteria.model_copy(update={"rules": [named_after_a_company]})
    storage = setup(tmp_path, active())
    summary = day(storage, crit, {"ashby": Board(["Director, Product", "Director, Engineering"])})

    assert summary.fetch.results[0].rejected == {
        "rule:zephyr-no-product-roles": 1
    }  # the raw bucket does carry the name
    assert summary.rejected == {"rules": 1}  # ...but the public summary only says "rules"
    assert "zephyr" not in summary.to_markdown().lower() and "rules 1" in summary.to_markdown()


def test_the_markdown_is_written_for_the_github_summary_page(tmp_path, criteria):
    summary = day(setup(tmp_path, active()), criteria, {"ashby": Board(["Director, Engineering"])})
    assert summary.to_markdown().startswith("## Daily run") and summary.finished is not None


# --- the resume can come from a secret ------------------------------------------------------


def test_the_resume_secret_wins_over_the_file(tmp_path):
    secret = yaml.safe_dump({"summary": "from the secret"})
    assert load_master_resume(
        Settings(master_resume_yaml=SecretStr(secret)), tmp_path / "missing.yaml"
    ) == {"summary": "from the secret"}


def test_without_the_secret_the_file_is_used(tmp_path):
    f = tmp_path / "r.yaml"
    f.write_text("summary: from the file\n")
    assert load_master_resume(Settings(master_resume_yaml=None), f) == {"summary": "from the file"}
