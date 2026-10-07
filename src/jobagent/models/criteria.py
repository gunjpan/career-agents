import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Unknown keys are errors, so a typo in criteria.yaml fails loudly instead of doing nothing."""

    model_config = ConfigDict(extra="forbid")


def _check_regexes(patterns: list[str]) -> list[str]:
    for p in patterns:
        try:
            re.compile(p)
        except re.error as e:
            raise ValueError(f"invalid regex {p!r}: {e}") from e
    return patterns


class Rule(StrictModel):
    """One filter, as data. A closed vocabulary (no expressions) keeps rules safe and testable.

    exclude: reject the posting when the field matches.
    include: when any include rule applies to a field, at least one of them must match.
    flag:    keep the posting, but mark it in the Jobs tab's flags column ("flag:<name>").
    """

    name: str = Field(min_length=1)
    field: Literal["title", "department", "company", "workplace_type"]
    match: str  # regex, case-insensitive, searched anywhere in the field value
    action: Literal["exclude", "include", "flag"]
    companies: list[str] = Field(default_factory=list)  # empty = applies to every company

    @field_validator("match")
    @classmethod
    def _valid_regex(cls, v: str) -> str:
        return _check_regexes([v])[0]

    def applies_to(self, company: str) -> bool:
        return not self.companies or company.lower() in {c.lower() for c in self.companies}


class LocationCriteria(StrictModel):
    include_patterns: list[str]  # regexes, case-insensitive, matched against each location string
    allow_remote: bool = True
    remote_country_patterns: list[str] = Field(default_factory=list)  # e.g. "canada"
    foreign_markers: list[str] = Field(default_factory=list)  # remote roles tied to these are out

    _regexes = field_validator("include_patterns", "remote_country_patterns", "foreign_markers")(
        _check_regexes
    )


class DiscoveryCriteria(StrictModel):
    """How adapters that must search (Workday) narrow the posting list at the source."""

    search_terms: list[str] = Field(default_factory=list)  # one search per term, results merged
    country: str | None = None  # server-side country filter, where the ATS supports one
    # Regexes (case-insensitive) matched against the ATS's top-level job family names. Empty =
    # no family filter. Names differ per company, hence patterns and not ids.
    job_family_patterns: list[str] = Field(default_factory=list)

    _regexes = field_validator("job_family_patterns")(_check_regexes)


class Criteria(StrictModel):
    """Hard filters. Run before any LLM call."""

    locations: LocationCriteria
    rules: list[Rule] = Field(default_factory=list)
    discovery: DiscoveryCriteria = Field(default_factory=DiscoveryCriteria)
    max_age_days: int = 30
    keep_undated: bool = True  # a posting with no date is kept and flagged, never dropped

    @model_validator(mode="after")
    def _unique_rule_names(self) -> "Criteria":
        names = [r.name for r in self.rules]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate rule names: {dupes}")  # names label the run summary
        return self


def load_criteria(path: str | Path = "config/criteria.yaml") -> Criteria:
    with open(path) as f:
        return Criteria.model_validate(yaml.safe_load(f))
