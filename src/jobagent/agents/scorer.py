from datetime import UTC, datetime

from jobagent.agents.context import render_posting
from jobagent.agents.prompts import Prompt
from jobagent.llm.base import LLMProvider, LLMValidationError
from jobagent.models.job import Job
from jobagent.models.scoring import ScoreResult, ScorerOutput, ScoringConfig


class Scorer:
    """Level + fit scoring for one posting. Judgment only: thresholds and state changes are
    decided in code (orchestrator), not here."""

    def __init__(
        self,
        provider: LLMProvider,
        config: ScoringConfig,
        prompt: Prompt,
        resume_text: str,
        profiles_text: str,
    ) -> None:
        self.provider = provider
        self.config = config
        self.prompt = prompt
        # Same for every posting in a run, so the provider can cache it.
        self.prefix = f"{prompt.text}\n\n{profiles_text}\n\n{resume_text}"

    def build_user_message(self, job: Job) -> str:
        return render_posting(job, self.config.max_description_chars)

    def score(self, job: Job) -> ScoreResult:
        """Raises LLMError if the call fails; retries once if the output fails validation."""
        message = self.build_user_message(job)
        for attempt in (1, 2):
            try:
                result = self.provider.complete(
                    model=self.config.model,
                    cacheable_prefix=self.prefix,
                    prompt=message,
                    schema=ScorerOutput,
                    max_tokens=self.config.max_tokens,
                    temperature=self.config.temperature,
                )
                break
            except LLMValidationError:
                if attempt == 2:
                    raise
        usage = result.usage
        return ScoreResult(
            output=result.parsed,
            prompt_version=self.prompt.version,
            model=result.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=usage.cost_usd(self.config.pricing()),
            scored_at=datetime.now(UTC),
        )
