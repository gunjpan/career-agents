from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class LocationCriteria(BaseModel):
    include_patterns: list[str]  # regexes, case-insensitive, matched against each location string
    allow_remote: bool = True
    remote_country_patterns: list[str] = Field(default_factory=list)  # e.g. "canada"
    foreign_markers: list[str] = Field(default_factory=list)  # remote roles tied to these are out


class TitleCriteria(BaseModel):
    exclude_patterns: list[str] = Field(default_factory=list)  # regexes, case-insensitive


class Criteria(BaseModel):
    """Hard filters. Run before any LLM call."""

    locations: LocationCriteria
    titles: TitleCriteria
    max_age_days: int = 30
    keep_undated: bool = True  # a posting with no date is kept and flagged, never dropped


def load_criteria(path: str | Path = "config/criteria.yaml") -> Criteria:
    with open(path) as f:
        return Criteria.model_validate(yaml.safe_load(f))
