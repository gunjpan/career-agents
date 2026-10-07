from types import SimpleNamespace

import anthropic
import httpx
import pytest

from jobagent.agents.context import render_profiles, render_resume
from jobagent.agents.prompts import Prompt, load_prompt
from jobagent.agents.scorer import Scorer
from jobagent.llm.base import LLMError, LLMResult, LLMValidationError, Usage
from jobagent.llm.claude import ClaudeProvider
from jobagent.models.job import JOBS_HEADERS, Job, JobState
from jobagent.models.scoring import ScorerOutput, ScoringConfig, load_scoring_config
from jobagent.orchestrator.dedupe import dedupe_key
from jobagent.orchestrator.scoring import is_shortlisted, run_scoring
from jobagent.storage.csv_store import CsvStorage

from .conftest import NOW, posting

RESUME = {
    "profile": {"phone": "555-0100", "email": "me@example.com", "linkedin": "linkedin.com/in/me"},
    "summary": "Engineering leader.",
    "core_strengths": ["Delivery", "Architecture"],
    "experience": [
        {
            "company": "Acme",
            "start": "2020-01",
            "end": "2024-01",
            "tech_stack": ["Python", "Kubernetes"],
            "roles": [
                {
                    "title": "Director",
                    "start": "2021-01",
                    "end": "2024-01",
                    "scope": {"engineers": "30+"},
                    "skills": ["leadership"],
                    "bullets": [{"id": "a-1", "text": "Led  a   team\nof 30."}],
                }
            ],
        }
    ],
    "current_focus": {"title": "AI", "bullets": [{"id": "ai-1", "text": "Building a RAG app."}]},
    "education": [{"degree": "B.Eng.", "school": "Some University"}],
}


def output(**kw) -> ScorerOutput:
    base = {
        "real_level": "director",
        "level_confidence": "high",
        "level_evidence": ["manages managers", "40+ engineers"],
        "title_matches_level": True,
        "function": "engineering",
        "core_domain_covered": True,
        "fit_score": 82,
        "rationale": "Strong match.",
        "strengths": ["delivery at scale"],
        "gaps": ["no Rust"],
    }
    return ScorerOutput(**{**base, **kw})


class FakeProvider:
    """Stands in for Claude: returns canned outputs, records calls, can fail on demand."""

    def __init__(self, outputs=None, fail=None):
        self.outputs = list(outputs or [])
        self.fail = list(fail or [])  # exceptions raised first, one per call
        self.calls: list[dict] = []

    def complete(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail.pop(0)
        out = self.outputs.pop(0) if self.outputs else output()
        return LLMResult(out, Usage(1000, 300, cache_read_tokens=2000), "claude-haiku-4-5")


@pytest.fixture
def config() -> ScoringConfig:
    return load_scoring_config("config/scoring.yaml")


@pytest.fixture
def scorer(config):
    def make(provider):
        return Scorer(provider, config, Prompt(7, "PROMPT"), "RESUME-TEXT", "PROFILES-TEXT")

    return make


def filtered_job(job_id="j1", title="Director, Engineering", **kw) -> Job:
    p = posting(title=title, external_id=job_id, description="Lead 40 engineers.", **kw)
    return Job(
        job_id=job_id,
        dedupe_key=dedupe_key(p),
        state=JobState.FILTERED,
        first_seen=NOW,
        posting=p,
    )


def storage_with(tmp_path, *jobs: Job) -> CsvStorage:
    s = CsvStorage(str(tmp_path))
    s.append_records("Jobs", JOBS_HEADERS, [j.to_record() for j in jobs])
    return s


# --- config, prompt, context ---------------------------------------------------------------


def test_scoring_config_loads_and_prices_the_model(config):
    assert config.model == "claude-haiku-4-5"
    assert config.pricing().input == 1.00 and config.pricing().output == 5.00


def test_prompt_version_comes_from_front_matter(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("---\nversion: 3\n---\nHello\n")
    assert load_prompt(f) == Prompt(3, "Hello")
    assert load_prompt("prompts/scorer.md").version >= 1
    f.write_text("no front matter")
    with pytest.raises(ValueError, match="front matter"):
        load_prompt(f)


def test_resume_text_has_experience_but_never_contact_details():
    text = render_resume(RESUME)
    assert "Led a team of 30." in text and "Python, Kubernetes" in text and "30+" in text
    for secret in ("555-0100", "me@example.com", "linkedin.com/in/me"):
        assert secret not in text


def test_profiles_render_levels_and_inflation_notes():
    text = render_profiles(
        {
            "levels": {"vp": {"title_variants": ["VP"], "scope_signals": {"team": "100+"}}},
            "title_inflation_notes": ["Titles lie."],
        }
    )
    assert "## vp" in text and "- team: 100+" in text and "Titles lie." in text


def test_real_profiles_cover_every_shortlist_level(config):
    from jobagent.agents.context import load_yaml

    levels = load_yaml("config/role_profiles.yaml")["levels"]
    assert set(config.shortlist.levels) <= set(levels)


# --- cost ------------------------------------------------------------------------------------


def test_cost_counts_each_token_class_at_its_own_rate(config):
    usage = Usage(1_000_000, 100_000, cache_read_tokens=2_000_000, cache_write_tokens=1_000_000)
    # 1M*$1 + 0.1M*$5 + 2M*$0.10 + 1M*$1.25
    assert usage.cost_usd(config.pricing()) == pytest.approx(1.00 + 0.50 + 0.20 + 1.25)


# --- Scorer agent ----------------------------------------------------------------------------


def test_scorer_sends_stable_prefix_and_posting_in_tags(scorer, config):
    provider = FakeProvider()
    result = scorer(provider).score(filtered_job())
    call = provider.calls[0]

    assert call["cacheable_prefix"] == "PROMPT\n\nPROFILES-TEXT\n\nRESUME-TEXT"
    assert call["model"] == config.model and call["schema"] is ScorerOutput
    assert call["prompt"].startswith("<posting>\n") and call["prompt"].endswith("</posting>")
    assert (
        "Title: Director, Engineering" in call["prompt"] and "Lead 40 engineers." in call["prompt"]
    )
    assert result.prompt_version == 7 and result.output.fit_score == 82
    assert result.cost_usd == pytest.approx((1000 * 1.0 + 300 * 5.0 + 2000 * 0.10) / 1e6)


def test_scorer_truncates_very_long_postings(scorer, config):
    job = filtered_job()
    job.posting.description = "x" * (config.max_description_chars + 500)
    message = scorer(FakeProvider()).build_user_message(job)
    assert "[posting truncated]" in message and message.count("x") == config.max_description_chars


def test_scorer_retries_once_on_invalid_output_then_succeeds(scorer):
    provider = FakeProvider(fail=[LLMValidationError("bad json")])
    assert scorer(provider).score(filtered_job()).output.fit_score == 82
    assert len(provider.calls) == 2


def test_scorer_gives_up_after_a_second_invalid_output(scorer):
    provider = FakeProvider(fail=[LLMValidationError("1"), LLMValidationError("2")])
    with pytest.raises(LLMValidationError):
        scorer(provider).score(filtered_job())
    assert len(provider.calls) == 2


def test_scorer_does_not_retry_api_errors(scorer):
    provider = FakeProvider(fail=[LLMError("overloaded")])
    with pytest.raises(LLMError):
        scorer(provider).score(filtered_job())
    assert len(provider.calls) == 1


def test_fit_score_outside_0_100_is_invalid():
    with pytest.raises(ValueError, match="0-100"):
        output(fit_score=101)


# --- shortlist decision ----------------------------------------------------------------------


def test_shortlist_needs_fit_level_and_function_together(config):
    from jobagent.models.scoring import ScoreResult

    def sr(**kw):
        return ScoreResult(
            output=output(**kw), prompt_version=1, model="m", input_tokens=1, output_tokens=1,
            cost_usd=0.0, scored_at=NOW,
        )  # fmt: skip

    assert is_shortlisted(sr(), config)
    assert not is_shortlisted(sr(fit_score=69), config)  # below min_fit
    assert is_shortlisted(sr(fit_score=70), config)  # boundary
    assert not is_shortlisted(sr(real_level="manager"), config)  # level not targeted
    assert not is_shortlisted(sr(function="product"), config)  # function not targeted
    # A high score does not shortlist a role whose central domain the profile doesn't cover.
    assert not is_shortlisted(sr(fit_score=95, core_domain_covered=False), config)


# --- Job <-> row ---------------------------------------------------------------------------


def test_job_survives_a_round_trip_through_a_row():
    job = filtered_job(locations=["Toronto, ON", "Remote (Canada)"], department="Technology")
    back = Job.from_record(job.to_record())
    assert back.posting.title == job.posting.title
    assert back.posting.locations == ["Toronto, ON", "Remote (Canada)"]
    assert back.posting.department == "Technology" and back.state == JobState.FILTERED


# --- run_scoring -----------------------------------------------------------------------------


def test_run_scoring_scores_filtered_jobs_and_shortlists(tmp_path, scorer, config):
    storage = storage_with(tmp_path, filtered_job("a"), filtered_job("b", "Senior Manager, QA"))
    provider = FakeProvider([output(), output(real_level="manager", fit_score=40)])

    summary = run_scoring(storage, scorer(provider), config)

    assert (summary.scored, summary.shortlisted, summary.failed) == (2, 1, 0)
    rows = {r["job_id"]: r for r in storage.read_records("Jobs")}
    assert rows["a"]["state"] == "shortlisted" and rows["a"]["fit_score"] == "82"
    assert rows["b"]["state"] == "scored" and rows["b"]["real_level"] == "manager"
    assert rows["a"]["title"] == "Director, Engineering"  # other columns untouched
    assert rows["a"]["scorer_prompt_version"] == "7" and rows["a"]["function"] == "engineering"
    assert summary.cost_usd == pytest.approx(2 * (1000 + 1500 + 200) / 1e6)
    assert summary.by_level == {"director": 1, "manager": 1}


def test_run_scoring_only_touches_filtered_jobs_and_is_idempotent(tmp_path, scorer, config):
    done = filtered_job("done").model_copy(update={"state": JobState.SCORED})
    storage = storage_with(tmp_path, done, filtered_job("new"))
    provider = FakeProvider()

    run_scoring(storage, scorer(provider), config)
    assert len(provider.calls) == 1  # the already-scored job was not sent again

    second = run_scoring(storage, scorer(provider), config)
    assert second.scored == 0 and len(provider.calls) == 1


def test_failed_job_stays_filtered_for_the_next_run(tmp_path, scorer, config):
    storage = storage_with(tmp_path, filtered_job("a"), filtered_job("b"))
    provider = FakeProvider(fail=[LLMError("boom")])

    summary = run_scoring(storage, scorer(provider), config)

    assert (summary.scored, summary.failed) == (1, 1) and "boom" in summary.errors[0]
    states = {r["job_id"]: r["state"] for r in storage.read_records("Jobs")}
    assert states["a"] == "filtered" and states["b"] in {"scored", "shortlisted"}


def test_limit_caps_how_many_jobs_are_scored(tmp_path, scorer, config):
    storage = storage_with(tmp_path, *[filtered_job(f"j{i}") for i in range(4)])
    summary = run_scoring(storage, scorer(FakeProvider()), config, limit=2)
    assert summary.scored == 2 and summary.remaining == 2


def test_only_scores_the_requested_job_ids(tmp_path, scorer, config):
    storage = storage_with(tmp_path, filtered_job("a"), filtered_job("b"), filtered_job("c"))
    provider = FakeProvider()
    summary = run_scoring(storage, scorer(provider), config, only={"b"})
    assert summary.scored == 1 and len(provider.calls) == 1
    states = {r["job_id"]: r["state"] for r in storage.read_records("Jobs")}
    assert states["a"] == states["c"] == "filtered" and states["b"] != "filtered"


def test_spend_cap_stops_the_run_and_leaves_the_rest_filtered(tmp_path, scorer, config):
    capped = config.model_copy(update={"max_run_cost_usd": 0.0003})  # ~one call's cost
    storage = storage_with(tmp_path, *[filtered_job(f"j{i}") for i in range(5)])

    summary = run_scoring(storage, scorer(FakeProvider()), capped)

    assert 1 <= summary.scored < 5 and "spend cap" in summary.stopped
    assert summary.remaining == 5 - summary.scored
    left = [r for r in storage.read_records("Jobs") if r["state"] == "filtered"]
    assert len(left) == summary.remaining


def test_scores_already_paid_for_are_saved_even_if_the_run_crashes(tmp_path, scorer, config):
    storage = storage_with(tmp_path, filtered_job("a"), filtered_job("b"))

    class Crashy(FakeProvider):
        def complete(self, **kw):
            if len(self.calls) == 1:
                raise KeyboardInterrupt  # the user hit Ctrl+C on the second job
            return super().complete(**kw)

    with pytest.raises(KeyboardInterrupt):
        run_scoring(storage, scorer(Crashy()), config)
    states = {r["job_id"]: r["state"] for r in storage.read_records("Jobs")}
    assert states["a"] in {"scored", "shortlisted"} and states["b"] == "filtered"


def test_jobs_tab_gains_score_columns_without_losing_rows(tmp_path, scorer, config):
    storage = CsvStorage(str(tmp_path))
    old = JOBS_HEADERS[:15]  # the Jobs tab as it looked before Block 3
    storage.append_records("Jobs", old, [filtered_job("a").to_record()])

    run_scoring(storage, scorer(FakeProvider()), config)

    [row] = storage.read_records("Jobs")
    assert row["title"] == "Director, Engineering" and row["fit_score"] == "82"


# --- Claude provider (SDK stubbed) -----------------------------------------------------------


def stub_client(*, parsed=None, stop="end_turn", raises=None):
    usage = SimpleNamespace(
        input_tokens=11,
        output_tokens=22,
        cache_read_input_tokens=33,
        cache_creation_input_tokens=44,
    )
    response = SimpleNamespace(
        parsed_output=parsed, stop_reason=stop, usage=usage, model="claude-haiku-4-5"
    )

    def parse(**kwargs):
        parse.kwargs = kwargs
        if raises:
            raise raises
        return response

    return SimpleNamespace(messages=SimpleNamespace(parse=parse)), parse


def call(provider):
    return provider.complete(
        model="m", cacheable_prefix="PREFIX", prompt="P", schema=ScorerOutput, max_tokens=99
    )


def test_claude_provider_caches_the_prefix_and_maps_usage():
    client, parse = stub_client(parsed=output())
    result = call(ClaudeProvider(client=client))

    system = parse.kwargs["system"][0]
    assert system["text"] == "PREFIX" and system["cache_control"] == {"type": "ephemeral"}
    assert parse.kwargs["output_format"] is ScorerOutput
    assert parse.kwargs["extra_body"] == {"temperature": 0.0}
    assert result.usage == Usage(11, 22, cache_read_tokens=33, cache_write_tokens=44)


def test_claude_provider_omits_temperature_when_the_config_says_null():
    client, parse = stub_client(parsed=output())
    ClaudeProvider(client=client).complete(
        model="m", cacheable_prefix="P", prompt="p", schema=ScorerOutput, max_tokens=9,
        temperature=None,
    )  # fmt: skip
    assert "extra_body" not in parse.kwargs and "temperature" not in parse.kwargs


def test_claude_provider_turns_failures_into_llm_errors():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    api_error = anthropic.APIConnectionError(request=request)
    with pytest.raises(LLMError, match="APIConnectionError"):
        call(ClaudeProvider(client=stub_client(raises=api_error)[0]))
    with pytest.raises(LLMError, match="refused"):
        call(ClaudeProvider(client=stub_client(parsed=output(), stop="refusal")[0]))
    with pytest.raises(LLMValidationError):
        call(ClaudeProvider(client=stub_client(parsed=None, stop="max_tokens")[0]))
