from dataclasses import dataclass, field

from jobagent.agents.context import render_posting
from jobagent.agents.prompts import Prompt
from jobagent.agents.runner import AgentRun, call_agent
from jobagent.agents.tailor import Assessment
from jobagent.llm.base import LLMProvider, check_provider_policy
from jobagent.models.job import Job
from jobagent.models.scoring import Pricing
from jobagent.models.tailoring import TailoringConfig, TailorOutput, VerifierOutput


def render_documents(out: TailorOutput) -> str:
    """The Tailor's documents as plain text, with the ids each statement cites."""
    lines = ["SUMMARY"]
    lines += [
        f"[S{i}] {c.text}  (cites: {', '.join(c.supports)})" for i, c in enumerate(out.summary, 1)
    ]
    lines += ["", "EXPERIENCE"]
    for role in out.roles:
        lines.append(f"role {role.role_id}:")
        lines += [f"  [{b.source_id}] {b.text}" for b in role.bullets]
    lines += ["", "COVER LETTER"]
    lines += [
        f"[C{i}] {c.text}  (cites: {', '.join(c.supports)})"
        for i, c in enumerate(out.cover_letter, 1)
    ]
    return "\n".join(lines)


@dataclass
class Decision:
    passed: bool
    problems: list[str] = field(default_factory=list)  # what the Tailor must fix
    warnings: list[str] = field(default_factory=list)  # reported, but do not block


def decide(v: VerifierOutput) -> Decision:
    """Pass/fail is decided here, in code, not by the model."""
    problems = [
        f"{c.verdict}: {c.claim[:90]} - {c.note}" for c in v.checks if c.verdict != "traced"
    ]
    if not v.level_framing_ok:
        problems.append(f"level framing: {v.level_framing_note}")
    for fix in v.feedback:
        if fix not in problems:
            problems.append(fix)
    bad = any(c.verdict != "traced" for c in v.checks) or not v.level_framing_ok
    warnings = [f"must-have not addressed: {m}" for m in v.missing_must_haves]
    return Decision(passed=not bad, problems=problems if bad else [], warnings=warnings)


class Verifier:
    def __init__(
        self,
        provider: LLMProvider,
        config: TailoringConfig,
        prompt: Prompt,
        resume_text: str,  # rendered WITH bullet ids
        pricing: Pricing,
        max_description_chars: int = 12000,
    ) -> None:
        check_provider_policy(provider, public_data=False)  # these agents receive the resume
        self.provider, self.config, self.prompt, self.pricing = provider, config, prompt, pricing
        self.max_chars = max_description_chars
        self.prefix = f"{prompt.text}\n\n{resume_text}"

    def build_message(self, job: Job, a: Assessment, out: TailorOutput) -> str:
        return "\n\n".join(
            [
                render_posting(job, self.max_chars),
                f"<role_level>{a.real_level}</role_level>",
                f"<documents>\n{render_documents(out)}\n</documents>",
            ]
        )

    def run(self, job: Job, a: Assessment, out: TailorOutput) -> AgentRun[VerifierOutput]:
        return call_agent(
            self.provider, self.config.verifier, self.prompt, self.pricing,
            prefix=self.prefix, message=self.build_message(job, a, out), schema=VerifierOutput,
        )  # fmt: skip
