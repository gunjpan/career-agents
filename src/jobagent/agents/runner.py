from dataclasses import dataclass
from typing import Generic

from jobagent.agents.prompts import Prompt
from jobagent.llm.base import LLMProvider, LLMValidationError, T
from jobagent.models.scoring import Pricing
from jobagent.models.tailoring import AgentModel


@dataclass
class AgentRun(Generic[T]):  # noqa: UP046 - T is shared with LLMProvider.complete
    output: T
    model: str
    prompt_version: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


def call_agent(
    provider: LLMProvider,
    agent: AgentModel,
    prompt: Prompt,
    pricing: Pricing,
    *,
    prefix: str,
    message: str,
    schema: type[T],
) -> AgentRun[T]:
    """One schema-validated model call, retried once if the output is malformed."""
    for attempt in (1, 2):
        try:
            result = provider.complete(
                model=agent.model,
                cacheable_prefix=prefix,
                prompt=message,
                schema=schema,
                max_tokens=agent.max_tokens,
                temperature=agent.temperature,
                effort=agent.effort,
            )
            break
        except LLMValidationError:
            if attempt == 2:
                raise
    u = result.usage
    return AgentRun(
        output=result.parsed,
        model=result.model,
        prompt_version=prompt.version,
        input_tokens=u.input_tokens + u.cache_read_tokens + u.cache_write_tokens,
        output_tokens=u.output_tokens,
        cost_usd=u.cost_usd(pricing),
    )
