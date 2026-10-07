from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

Level = Literal[
    "individual_contributor", "manager", "senior_manager", "director", "vp", "executive"
]
Function = Literal[
    "engineering",
    "ai_data",
    "security",
    "it_operations",
    "product",
    "program_delivery",
    "risk_compliance",
    "business_other",
]
Confidence = Literal["low", "medium", "high"]


class ScorerOutput(BaseModel):
    """What the Scorer returns. Structured on purpose: every field is a feature for the
    classifier we may train later from your Apply/Skip labels."""

    real_level: Level
    level_confidence: Confidence
    level_evidence: list[str]  # the scope signals from the posting the level is based on
    title_matches_level: bool  # False when the title overstates or understates the real level
    function: Function
    # Is the role's central domain or technology evidenced in the candidate's profile? Asked as a
    # yes/no because models follow a clear question far more reliably than a score-capping rule.
    core_domain_covered: bool
    fit_score: int  # 0-100: how well the candidate's background matches this role
    rationale: str
    strengths: list[str]
    gaps: list[str]

    @field_validator("fit_score")
    @classmethod
    def _in_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(f"fit_score must be 0-100, got {v}")
        return v


class ScoreResult(BaseModel):
    """A ScorerOutput plus how it was produced, so every score is reproducible and costed."""

    output: ScorerOutput
    prompt_version: int
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float
    scored_at: datetime


# --- config/scoring.yaml -----------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pricing(_Strict):
    input: float
    output: float
    cache_read: float
    cache_write: float


class ShortlistRule(_Strict):
    min_fit: int = 70
    levels: list[Level]
    functions: list[Function]


class ScoringConfig(_Strict):
    model: str
    prompt: str = "prompts/scorer.md"
    max_tokens: int = 1500
    temperature: float | None = 0.0  # null for models that reject sampling parameters
    max_description_chars: int = 12000
    pricing_usd_per_mtok: dict[str, Pricing]
    shortlist: ShortlistRule
    max_run_cost_usd: float = Field(default=1.0, gt=0)

    def pricing(self) -> Pricing:
        try:
            return self.pricing_usd_per_mtok[self.model]
        except KeyError as e:
            raise ValueError(f"no pricing for model {self.model!r} in scoring.yaml") from e


def load_scoring_config(path: str | Path = "config/scoring.yaml") -> ScoringConfig:
    with open(path) as f:
        return ScoringConfig.model_validate(yaml.safe_load(f))
