import pytest
from pydantic import ValidationError

from jobagent.models.criteria import (
    EXAMPLE_CRITERIA,
    LOCAL_CRITERIA,
    Criteria,
    criteria_source,
    load_criteria,
)

MINIMAL = {"locations": {"include_patterns": ["toronto"]}}


def rule(**kw):
    return {"name": "r1", "field": "title", "match": "x", "action": "exclude", **kw}


def test_real_config_loads(criteria):
    assert criteria.discovery.search_terms
    assert {r.action for r in criteria.rules} == {"exclude", "include", "flag"}


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ValidationError, match="max_age_day"):
        Criteria.model_validate({**MINIMAL, "max_age_day": 45})  # typo of max_age_days


def test_old_titles_section_is_gone():
    with pytest.raises(ValidationError, match="titles"):
        Criteria.model_validate({**MINIMAL, "titles": {"exclude_patterns": ["avp"]}})


def test_unknown_key_inside_a_rule_is_rejected():
    with pytest.raises(ValidationError, match="compnies"):
        Criteria.model_validate({**MINIMAL, "rules": [rule(compnies=["Acme"])]})


def test_rule_vocabulary_is_closed():
    for bad in ({"field": "salary"}, {"action": "boost"}):
        with pytest.raises(ValidationError):
            Criteria.model_validate({**MINIMAL, "rules": [rule(**bad)]})


def test_invalid_regex_is_rejected_at_load_time():
    with pytest.raises(ValidationError, match="invalid regex"):
        Criteria.model_validate({**MINIMAL, "rules": [rule(match="(unclosed")]})
    bad_family = {**MINIMAL, "discovery": {"job_family_patterns": ["[bad"]}}
    with pytest.raises(ValidationError, match="invalid regex"):
        Criteria.model_validate(bad_family)


def test_duplicate_rule_names_are_rejected():
    with pytest.raises(ValidationError, match="duplicate rule names"):
        Criteria.model_validate({**MINIMAL, "rules": [rule(), rule(match="y")]})


def test_load_criteria_reports_the_bad_file(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("locations: {include_patterns: [x]}\nbogus: 1\n")
    with pytest.raises(ValidationError, match="bogus"):
        load_criteria(f)


# --- where the criteria come from ---------------------------------------------------------------

import yaml
from pydantic import SecretStr

from jobagent.settings import Settings

SECRET = yaml.safe_dump({"locations": {"include_patterns": ["narnia"]}, "max_age_days": 7})


def test_the_ci_secret_wins_over_any_file(monkeypatch):
    settings = Settings(criteria_yaml=SecretStr(SECRET))
    crit = load_criteria(settings=settings)
    assert crit.max_age_days == 7 and criteria_source(settings) == "CRITERIA_YAML secret"


def test_an_empty_secret_is_ignored(
    monkeypatch,
):  # an unset GitHub secret arrives as an empty string
    settings = Settings(criteria_yaml=SecretStr("  "))
    assert (
        criteria_source(settings) != "CRITERIA_YAML secret"
        and load_criteria(settings=settings).rules is not None
    )


def test_a_bad_secret_is_an_error_never_a_silent_fallback_to_the_example():
    with pytest.raises(ValidationError):
        load_criteria(
            settings=Settings(
                criteria_yaml=SecretStr("locations: {include_patterns: [x]}\nbogus: 1\n")
            )
        )


def test_the_local_file_beats_the_example(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "criteria.local.yaml").write_text(
        "locations: {include_patterns: [local]}\nmax_age_days: 3\n"
    )
    (tmp_path / "config" / "criteria.example.yaml").write_text(
        "locations: {include_patterns: [example]}\nmax_age_days: 99\n"
    )
    monkeypatch.chdir(tmp_path)
    assert load_criteria().max_age_days == 3
    (tmp_path / "config" / "criteria.local.yaml").unlink()
    assert load_criteria().max_age_days == 99


def test_an_explicit_path_always_wins(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("locations: {include_patterns: [x]}\nmax_age_days: 5\n")
    assert load_criteria(f, Settings(criteria_yaml=SecretStr(SECRET))).max_age_days == 5


def test_the_committed_example_is_valid_and_names_only_a_fictional_company():
    crit = load_criteria(EXAMPLE_CRITERIA)
    scoped = {c for r in crit.rules for c in r.companies}
    assert scoped == {"Example Corp"}  # nothing that reveals who you are targeting
    text = EXAMPLE_CRITERIA.read_text().lower()
    for real_name in ("wealthsimple", "rbc", "bmo", "cibc", "royal bank", "scotia"):
        assert real_name not in text


def test_the_private_local_file_if_present_is_valid_and_git_ignored():
    if not LOCAL_CRITERIA.exists():
        pytest.skip("no private criteria.local.yaml on this machine")
    load_criteria(LOCAL_CRITERIA)  # catches typos in your own file the moment you run the tests
    assert "criteria.local.yaml" in (LOCAL_CRITERIA.parents[1] / ".gitignore").read_text()
