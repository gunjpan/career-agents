from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class FetchPolicy(BaseModel):
    """How often a company's job board may be fetched. Keeps the tool a polite client."""

    model_config = ConfigDict(extra="forbid")

    default_min_hours: float = Field(default=6, ge=0)
    min_hours: dict[str, float] = Field(default_factory=dict)  # per ATS, e.g. {"workday": 20}

    def cooldown(self, ats: str) -> timedelta:
        return timedelta(hours=self.min_hours.get(ats, self.default_min_hours))

    def wait_remaining(self, ats: str, last_fetched: datetime | None, now: datetime) -> timedelta:
        """Zero when the company may be fetched now (never fetched, or the cooldown has passed)."""
        if last_fetched is None:
            return timedelta(0)
        if last_fetched.tzinfo is None:  # a timestamp typed by hand into the Sheet: assume UTC
            last_fetched = last_fetched.replace(tzinfo=UTC)
        return max(timedelta(0), last_fetched + self.cooldown(ats) - now)


def load_fetch_policy(path: str | Path = "config/fetch.yaml") -> FetchPolicy:
    with open(path) as f:
        return FetchPolicy.model_validate(yaml.safe_load(f))
