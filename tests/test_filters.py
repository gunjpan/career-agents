from datetime import UTC, datetime

from jobagent.models.criteria import Criteria
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
    assert not r.passed and r.bucket == "location"


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


def test_inflated_titles_rejected_and_attributed_to_the_rule(criteria, now):
    for title in (
        "Assistant Vice President, Lead Developer",
        "Associate Vice-President, Software Engineering",
        "AVP - Platform Engineering",
    ):
        r = check(posting(title=title), criteria, now)
        assert not r.passed and r.bucket == "rule:no-inflated-vp-titles", title


def test_real_vp_title_not_excluded_by_regex(criteria, now):
    assert check(posting(title="Vice President, Engineering"), criteria, now).passed


def test_old_posting_rejected(criteria, now):
    old = posting(posted_at=datetime(2026, 8, 1, tzinfo=UTC))
    r = check(old, criteria, now)
    assert not r.passed and r.bucket == "age"


def test_boundary_29_days_passes(criteria, now):
    assert check(posting(posted_at=datetime(2026, 9, 7, tzinfo=UTC)), criteria, now).passed


def test_undated_kept_and_flagged(criteria, now):
    r = check(posting(posted_at=None), criteria, now)
    assert r.passed and "date_unknown" in r.flags


# --- rules ---------------------------------------------------------------------------------


def crit(*rules: dict) -> Criteria:
    return Criteria.model_validate(
        {"locations": {"include_patterns": ["toronto"]}, "rules": list(rules)}
    )


def r(name="r", field="title", match="x", action="exclude", **kw) -> dict:
    return {"name": name, "field": field, "match": match, "action": action, **kw}


def test_exclude_rule_on_department(now):
    c = crit(r("no-product", "department", "^product$"))
    out = check(posting(department="Product"), c, now)
    assert not out.passed and out.bucket == "rule:no-product"
    assert check(posting(department="Product Security"), c, now).passed  # anchored regex


def test_rule_scoped_to_companies_ignores_others(now):
    c = crit(r("only-acme", "department", "^product$", companies=["acme"]))  # case-insensitive
    assert not check(posting(company="Acme", department="Product"), c, now).passed
    assert check(posting(company="Other", department="Product"), c, now).passed


def test_flag_rule_keeps_the_posting_and_marks_it(now):
    c = crit(r("contract-role", match="contract", action="flag"))
    out = check(posting(title="Director, Engineering (Contract)"), c, now)
    assert out.passed and "flag:contract-role" in out.flags
    assert check(posting(), c, now).flags == []


def test_include_rule_requires_a_match_when_the_field_is_known(now):
    c = crit(r("tech-only", "department", "technology|data", action="include"))
    assert check(posting(department="Technology Solutions"), c, now).passed
    out = check(posting(department="Sales"), c, now)
    assert not out.passed and out.bucket == "include:department"


def test_include_rules_on_one_field_are_alternatives(now):
    c = crit(
        r("tech", "department", "technology", action="include"),
        r("ai", "department", "artificial", action="include"),
    )
    assert check(posting(department="Artificial Intelligence"), c, now).passed
    assert not check(posting(department="Sales"), c, now).passed


def test_include_rule_never_drops_when_the_field_is_empty(now):
    c = crit(r("tech-only", "department", "technology", action="include"))
    out = check(posting(department=""), c, now)
    assert out.passed and "department_unknown" in out.flags


def test_exclude_beats_include_and_rule_order_does_not_matter(now):
    inc = r("tech", "department", "technology", action="include")
    exc = r("no-lead", "title", "lead")
    for rules in ((inc, exc), (exc, inc)):
        out = check(posting(title="Tech Lead", department="Technology"), crit(*rules), now)
        assert not out.passed and out.bucket == "rule:no-lead"


def test_real_config_limits_wealthsimple_to_data_and_engineering(criteria, now):
    ok = posting(company="Wealthsimple", department="Data & Engineering")
    assert check(ok, criteria, now).passed
    for dept in ("Product", "Commercial & Marketing", "Operations"):
        out = check(posting(company="Wealthsimple", department=dept), criteria, now)
        assert not out.passed and out.bucket == "include:department", dept
    # Unknown department is never dropped, only flagged.
    unknown = check(posting(company="Wealthsimple", department=""), criteria, now)
    assert unknown.passed and "department_unknown" in unknown.flags


def test_real_config_wealthsimple_include_does_not_touch_other_companies(criteria, now):
    assert check(posting(company="RBC", department="Product"), criteria, now).passed
    assert check(posting(company="TD", department="Sales"), criteria, now).passed


def test_real_config_flags_fixed_term_roles(criteria, now):
    out = check(posting(title="Associate Director (Fixed-Term Contract)"), criteria, now)
    assert out.passed and "flag:contract-role" in out.flags


# --- title rules with the real config ---------------------------------------------------------


def test_leadership_titles_pass(criteria, now):
    for title in (
        "Director, Engineering",
        "Senior Director, AI Platform",
        "Associate Director, Data Engineering",
        "Senior Manager, Cloud Infrastructure",
        "Sr. Manager, Software Development",
        "Vice President, Technology",
        "VP of Engineering",
        "Head of AI",
        "Chief Technology Officer",
    ):
        assert check(posting(title=title), criteria, now).passed, title


def test_engineering_manager_titles_pass(criteria, now):
    for title in (
        "Manager, Software Development - Financial Risk",
        "Manager Software Development, Observability Platform",
        "Data Platform Engineering Manager",
        "Manager, Engineering - Digital Onboarding Platform",
    ):
        assert check(posting(title=title), criteria, now).passed, title


def test_individual_contributor_titles_are_rejected(criteria, now):
    for title in (
        "Senior Data Engineer",
        "Staff Data Engineer",
        "Principal Engineer, Cyber Technology Operations",
        "Lead Full Stack Developer",
        "Senior AI/ML Engineer",
        "Senior Risk Analyst",
        "Technical Program Manager",
        "Penetration Tester",
    ):
        out = check(posting(title=title), criteria, now)
        assert not out.passed and out.bucket == "include:title", title


def test_non_technical_managers_are_rejected_not_matched_by_substring(criteria, now):
    # The engineering-manager rule must not match "AI" inside other words.
    for title in (
        "Manager, Airport Operations",
        "Manager, Aircraft Maintenance",
        "Dubai Manager",
        "Manager, Business Enterprise Systems",
        "Retail Manager",
        "Product Manager, Payments",
    ):
        out = check(posting(title=title), criteria, now)
        assert not out.passed and out.bucket == "include:title", title


def test_lead_developer_hidden_behind_a_director_title_is_excluded(criteria, now):
    out = check(posting(title="Associate Director - Murex Reporting Lead Developer"), criteria, now)
    assert not out.passed and out.bucket == "rule:no-lead-developer-titles"
    # "Lead Engineering" in a leadership title is not "lead engineer".
    assert check(posting(title="Director, Lead Engineering Programs"), criteria, now).passed
