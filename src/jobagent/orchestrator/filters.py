import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jobagent.models.criteria import Criteria
from jobagent.models.job import RawPosting


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""  # why it was rejected (empty when passed)
    flags: list[str] = field(default_factory=list)  # kept, but missing info worth showing


def _any(patterns: list[str], texts: list[str]) -> bool:
    return any(re.search(p, t, re.IGNORECASE) for p in patterns for t in texts)


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
    return FilterResult(False, reason=f"location: {' | '.join(texts)}")


def _check_title(p: RawPosting, c: Criteria) -> FilterResult:
    if _any(c.titles.exclude_patterns, [p.title]):
        return FilterResult(False, reason=f"title: {p.title}")
    return FilterResult(True)


def _check_age(p: RawPosting, c: Criteria, now: datetime) -> FilterResult:
    if p.posted_at is None:
        return FilterResult(c.keep_undated, reason="undated", flags=["date_unknown"])
    posted = p.posted_at if p.posted_at.tzinfo else p.posted_at.replace(tzinfo=UTC)
    if now - posted > timedelta(days=c.max_age_days):
        return FilterResult(False, reason=f"age: posted {posted.date()}")
    return FilterResult(True)


def apply_hard_filters(p: RawPosting, c: Criteria, now: datetime) -> FilterResult:
    """Cheapest checks first. Runs before any LLM call."""
    flags: list[str] = []
    for result in (_check_title(p, c), _check_location(p, c), _check_age(p, c, now)):
        if not result.passed:
            return result
        flags += result.flags
    return FilterResult(True, flags=flags)
