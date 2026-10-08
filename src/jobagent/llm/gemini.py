import time
from collections.abc import Callable

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from jobagent.llm.base import LLMError, LLMResult, LLMValidationError, T, Usage

RETRY_STATUS = {429, 500, 502, 503, 504}


class GeminiProvider:
    """Gemini behind the LLMProvider interface.

    free_tier=True marks the provider public-data-only (see check_provider_policy): the free tier
    may use prompts for training, so only agents that handle public data may use it.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        free_tier: bool = True,
        client: genai.Client | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or genai.Client(api_key=api_key)
        self.public_data_only = free_tier
        self.max_retries = max_retries
        self.sleep = sleep

    def complete(
        self,
        *,
        model: str,
        cacheable_prefix: str,
        prompt: str,
        schema: type[T],
        max_tokens: int,
        temperature: float | None = 0.0,
        effort: str | None = None,  # Claude-specific; ignored here
    ) -> LLMResult[T]:
        config = types.GenerateContentConfig(
            system_instruction=cacheable_prefix,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )
        response = self._call_with_backoff(model, prompt, config)

        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise LLMError(f"prompt blocked: {feedback.block_reason}")
        try:
            parsed = response.parsed
        except (ValidationError, ValueError) as e:
            raise LLMValidationError(f"response did not match {schema.__name__}: {e}") from e
        if parsed is None:
            raise LLMValidationError("no parsed output (empty or non-JSON response)")

        u = response.usage_metadata
        usage = Usage(
            input_tokens=(u.prompt_token_count or 0) if u else 0,
            # Thinking tokens are billed as output, so count them.
            output_tokens=((u.candidates_token_count or 0) + (u.thoughts_token_count or 0))
            if u
            else 0,
            cache_read_tokens=(u.cached_content_token_count or 0) if u else 0,
        )
        return LLMResult(parsed, usage, model, getattr(response, "response_id", None))

    def _call_with_backoff(self, model: str, prompt: str, config: types.GenerateContentConfig):
        """Free-tier limits are tight: wait and retry on 429/5xx, then give up with a clear error."""
        for attempt in range(self.max_retries + 1):
            try:
                return self.client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code in RETRY_STATUS and attempt < self.max_retries:
                    self.sleep(min(2**attempt * 2, 30))  # 2s, 4s, 8s, 16s
                    continue
                hint = (
                    " (rate limit: the free tier allows few requests per minute/day)"
                    if code == 429
                    else ""
                )
                raise LLMError(f"Gemini {type(e).__name__} {code}: {str(e)[:160]}{hint}") from e
        raise LLMError("unreachable")  # pragma: no cover
