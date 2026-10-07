import pytest
from pydantic import ValidationError

from jobagent.models.criteria import Criteria, load_criteria

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
