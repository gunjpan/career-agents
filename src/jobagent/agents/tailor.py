from dataclasses import dataclass

from jobagent.agents.context import render_posting
from jobagent.agents.prompts import Prompt
from jobagent.agents.runner import AgentRun, call_agent
from jobagent.llm.base import LLMProvider, check_provider_policy
from jobagent.models.job import Job
from jobagent.models.scoring import Pricing
from jobagent.models.tailoring import TailoringConfig, TailorOutput


@dataclass(frozen=True)
class Assessment:
    """What the Scorer concluded about a posting, read back from the Jobs tab."""

    real_level: str
    function: str
    level_evidence: list[str]
    gaps: list[str]

    @classmethod
    def from_record(cls, rec: dict[str, str]) -> "Assessment":
        def split(key: str) -> list[str]:
            return [x for x in rec.get(key, "").split(" | ") if x]

        return cls(
            rec.get("real_level", "") or "unknown",
            rec.get("function", "") or "unknown",
            split("level_evidence"),
            split("gaps"),
        )


def level_guidance(profiles: dict, level: str) -> str:
    p = profiles.get("levels", {}).get(level)
    if not p:
        return "No level profile is defined for this level: use a neutral, factual tone."
    return f"Resume emphasis: {p['resume_emphasis']}\nCover letter tone: {p['cover_letter_tone']}"


class Tailor:
    def __init__(
        self,
        provider: LLMProvider,
        config: TailoringConfig,
        prompt: Prompt,
        resume_text: str,  # rendered WITH bullet ids
        profiles: dict,
        pricing: Pricing,
        max_description_chars: int = 12000,
    ) -> None:
        check_provider_policy(provider, public_data=False)  # these agents receive the resume
        self.provider, self.config, self.prompt = provider, config, prompt
        self.profiles, self.pricing = profiles, pricing
        self.max_chars = max_description_chars
        self.prefix = f"{prompt.text}\n\n{resume_text}"  # same for every job, so it can be cached

    def build_message(self, job: Job, a: Assessment, feedback: list[str] | None) -> str:
        parts = [
            render_posting(job, self.max_chars),
            "<role_assessment>\n"
            f"Real level: {a.real_level}\nFunction: {a.function}\n"
            + "".join(f"Scope signal: {e}\n" for e in a.level_evidence)
            + "Requirements the candidate does NOT show (never claim or hint at these):\n"
            + "".join(f"- {g}\n" for g in a.gaps)
            + "</role_assessment>",
            f"<level_guidance>\n{level_guidance(self.profiles, a.real_level)}\n</level_guidance>",
        ]
        if not self.config.cover_letter:
            parts.append(
                "<cover_letter>Not requested. Return an empty cover_letter list.</cover_letter>"
            )
        if feedback:
            parts.append(
                "<revision_feedback>\nYour previous draft had these problems. Fix all of them:\n"
                + "".join(f"- {f}\n" for f in feedback)
                + "</revision_feedback>"
            )
        return "\n\n".join(parts)

    def run(
        self, job: Job, a: Assessment, feedback: list[str] | None = None
    ) -> AgentRun[TailorOutput]:
        return call_agent(
            self.provider, self.config.tailor, self.prompt, self.pricing,
            prefix=self.prefix, message=self.build_message(job, a, feedback), schema=TailorOutput,
        )  # fmt: skip
