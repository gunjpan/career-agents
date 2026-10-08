from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Claim(BaseModel):
    """A sentence plus the master-resume bullet ids that back it. No ids, no claim."""

    text: str
    supports: list[str]


class TailoredBullet(BaseModel):
    source_id: str  # id of the master bullet this reworded bullet comes from
    text: str  # may be reworded for the job, but must not add facts


class TailoredRole(BaseModel):
    role_id: str  # id of the master role; title, employer and dates are copied verbatim
    bullets: list[TailoredBullet]


class TailorOutput(BaseModel):
    """What the Tailor returns. It selects and rewords; it cannot invent."""

    summary: list[Claim]  # 2-4 sentences, each backed by master bullet ids
    core_strengths: list[str]  # chosen verbatim from the master list
    roles: list[TailoredRole]  # most relevant roles first
    cover_letter: list[Claim]  # paragraphs, each backed by master bullet ids
    job_keywords_addressed: list[str]  # posting keywords the documents speak to (for evals)


ClaimVerdict = Literal["traced", "inflated", "unsupported", "wrong_level"]


class ClaimCheck(BaseModel):
    claim: str
    verdict: ClaimVerdict
    note: str  # why, when the verdict is not "traced"


class VerifierOutput(BaseModel):
    """What the Verifier returns. The pass/fail decision is made in code from these fields."""

    checks: list[ClaimCheck]  # one per bullet, summary sentence and cover-letter paragraph
    level_framing_ok: bool  # does the document present the candidate at the role's level?
    level_framing_note: str
    # Posting must-haves the master resume supports but the document leaves out.
    missing_must_haves: list[str]
    feedback: list[str]  # specific fixes for the Tailor, most important first


# --- config/tailoring.yaml ----------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentModel(_Strict):
    model: str
    prompt: str
    max_tokens: int = 6000
    temperature: float | None = None  # newer Sonnet models reject sampling parameters
    effort: Literal["low", "medium", "high"] | None = "medium"


class Limits(_Strict):
    max_bullets_per_role: int = 5
    max_total_bullets: int = 14
    max_summary_sentences: int = 4
    max_cover_paragraphs: int = 4


class TailoringConfig(_Strict):
    tailor: AgentModel
    verifier: AgentModel
    limits: Limits = Field(default_factory=Limits)
    # What the Tailor must keep. Anything it leaves out is added back from the master, word for word.
    #   all_bullets   - every bullet of every role appears; the Tailor only rewords and reorders
    #   all_roles     - every role appears, but the Tailor may drop bullets within a role
    #   tailor_choice - the Tailor decides what to include (bullet limits apply)
    keep: Literal["all_bullets", "all_roles", "tailor_choice"] = "all_bullets"
    cover_letter: bool = False  # off by default: few are read, and each adds claims to verify
    max_send_backs: int = 1  # Tailor gets this many corrections before the job is blocked
    max_run_cost_usd: float = Field(default=2.0, gt=0)
    drive_subfolder_template: str = "{company} - {title} - {job_id}"


def load_tailoring_config(path: str | Path = "config/tailoring.yaml") -> TailoringConfig:
    with open(path) as f:
        return TailoringConfig.model_validate(yaml.safe_load(f))
