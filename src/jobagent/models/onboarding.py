from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

AtsHint = Literal[
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "icims", "taleo",
    "oracle", "successfactors", "phenom", "own_site", "unknown",
]  # fmt: skip


class CareersGuess(BaseModel):
    """What the model says from its own knowledge. Treated as a hint: code verifies all of it."""

    known: bool  # does the model actually recognise this company?
    official_domain: str  # e.g. "wealthsimple.com"; empty if unknown
    careers_urls: list[str]  # up to 3 official pages that list jobs; empty if unknown
    ats_hint: AtsHint
    board_id_hint: str  # e.g. the Greenhouse/Lever/Ashby slug; empty if unknown
    notes: str


class OnboardingResult(BaseModel):
    """The outcome for one company, ready to write back to its Companies row."""

    status: Literal["active", "needs_review", "pending"]  # pending = try again later (model down)
    ats: str = ""
    board_id: str = ""
    reason: str  # always filled: why this status
    evidence: list[str] = []  # URLs and signals the decision rests on
    postings_found: int = 0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OnboardingConfig(_Strict):
    model: str
    prompt: str = "prompts/onboarding.md"
    free_tier: bool = True  # Gemini free tier: allowed because only public company names are sent
    max_tokens: int = 800
    max_candidate_urls: int = 3
    page_timeout_s: float = 15.0
    request_delay_s: float = 0.5  # politeness between fetches, and gentleness on the free tier


def load_onboarding_config(path: str | Path = "config/onboarding.yaml") -> OnboardingConfig:
    with open(path) as f:
        return OnboardingConfig.model_validate(yaml.safe_load(f))
