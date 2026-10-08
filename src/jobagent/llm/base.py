from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from jobagent.models.scoring import Pricing

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The call failed (API error, refusal, no usable output). The job is retried next run."""


class LLMValidationError(LLMError):
    """The model answered but not in the required schema. Worth one retry."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int  # uncached input only, as the API reports it
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost_usd(self, p: Pricing) -> float:
        return (
            self.input_tokens * p.input
            + self.output_tokens * p.output
            + self.cache_read_tokens * p.cache_read
            + self.cache_write_tokens * p.cache_write
        ) / 1_000_000


@dataclass
class LLMResult(Generic[T]):  # noqa: UP046 - T is shared with LLMProvider.complete
    parsed: T
    usage: Usage
    model: str
    request_id: str | None = None


class LLMProvider(Protocol):
    """One call, schema-validated. Claude today; Gemini or a local model behind the same
    interface later, so models can be compared by evals."""

    def complete(
        self,
        *,
        model: str,
        cacheable_prefix: str,  # identical across calls in a run: instructions, resume, profiles
        prompt: str,  # varies per call: the posting
        schema: type[T],
        max_tokens: int,
        temperature: float | None = 0.0,  # None = leave it to the model's default
        effort: str | None = None,  # thinking depth on models that support it; None = default
    ) -> LLMResult[T]: ...
