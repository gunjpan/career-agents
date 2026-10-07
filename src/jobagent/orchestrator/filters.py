import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jobagent.models.criteria import Criteria, Rule
from jobagent.models.job import RawPosting


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""  # why it was rejected, for humans (empty when passed)
    bucket: str = ""  # short key the run summary counts rejections under, e.g. "rule:no-avp"
    flags: list[str] = field(default_factory=list)  # kept, but worth showing in the Jobs tab


def _any(patterns: list[str], texts: list[str]) -> bool:
    return any(re.search(p, t, re.IGNORECASE) for p in patterns for t in texts)


def _field_value(p: RawPosting, name: str) -> str:
    return {
        "title": p.title,
        "department": p.department,
        "company": p.company,
        "workplace_type": p.workplace_type or "",
    }[name]


def _matches(rule: Rule, p: RawPosting) -> bool:
    return bool(re.search(rule.match, _field_value(p, rule.field), re.IGNORECASE))


def _check_rules(p: RawPosting, c: Criteria) -> FilterResult:
    rules = [r for r in c.rules if r.applies_to(p.company)]
    flags: list[str] = []

    for rule in (r for r in rules if r.action == "exclude"):
        if _matches(rule, p):
            value = _field_value(p, rule.field)
            return FilterResult(
                False, f"rule {rule.name}: {rule.field}={value!r}", f"rule:{rule.name}"
            )

    # Include rules are grouped per field: at least one of that field's rules must match.
    includes = [r for r in rules if r.action == "include"]
    for fld in dict.fromkeys(r.field for r in includes):
        group = [r for r in includes if r.field == fld]
        value = _field_value(p, fld)
        if not value:
            flags.append(f"{fld}_unknown")  # never drop on missing information
        elif not any(_matches(r, p) for r in group):
            names = ", ".join(r.name for r in group)
            return FilterResult(
                False, f"{fld}={value!r} matched no include rule ({names})", f"include:{fld}"
            )

    flags += [f"flag:{r.name}" for r in rules if r.action == "flag" and _matches(r, p)]
    return FilterResult(True, flags=flags)


def _check_location(p: RawPosting, c: Criteria) -> FilterResult:
    loc = c.locations
    texts = [t for t in p.locations if t]
    if _any(loc.include_patterns, texts):
        return FilterResult(True)
    is_remote = (
        p.remote is True
        or p.workplace_type in ("remote", "hybrid")
        or any("remote" in t.lower() or "hybrid" in t.lower() for t in texts)
    )
    if not texts:
        # No location data: never drop on missing information, but make it visible.
        return FilterResult(True, flags=["location_unknown"])
    if is_remote and loc.allow_remote:
        if _any(loc.remote_country_patterns, texts):
            return FilterResult(True)
        if not _any(loc.foreign_markers, texts):
            return FilterResult(True, flags=["remote_country_unknown"])
    return FilterResult(False, f"location: {' | '.join(texts)}", "location")


def _check_age(p: RawPosting, c: Criteria, now: datetime) -> FilterResult:
    if p.posted_at is None:
        return FilterResult(c.keep_undated, "undated", "age", flags=["date_unknown"])
    posted = p.posted_at if p.posted_at.tzinfo else p.posted_at.replace(tzinfo=UTC)
    if now - posted > timedelta(days=c.max_age_days):
        return FilterResult(False, f"age: posted {posted.date()}", "age")
    return FilterResult(True)


def apply_hard_filters(p: RawPosting, c: Criteria, now: datetime) -> FilterResult:
    """Cheapest checks first. Runs before any LLM call."""
    flags: list[str] = []
    for result in (_check_rules(p, c), _check_location(p, c), _check_age(p, c, now)):
        if not result.passed:
            return result
        flags += result.flags
    return FilterResult(True, flags=flags)
