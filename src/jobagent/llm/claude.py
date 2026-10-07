import anthropic
from pydantic import ValidationError

from jobagent.llm.base import LLMError, LLMResult, LLMValidationError, T, Usage


class ClaudeProvider:
    def __init__(
        self, api_key: str | None = None, client: anthropic.Anthropic | None = None
    ) -> None:
        # The SDK retries 429/5xx itself. With no api_key it falls back to ANTHROPIC_API_KEY.
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=3)

    def complete(
        self,
        *,
        model: str,
        cacheable_prefix: str,
        prompt: str,
        schema: type[T],
        max_tokens: int,
        temperature: float | None = 0.0,
    ) -> LLMResult[T]:
        # parse() has no sampling arguments, and newer Claude models reject them, so
        # temperature goes through extra_body and only when the config sets it.
        extra = {} if temperature is None else {"extra_body": {"temperature": temperature}}
        try:
            resp = self.client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                **extra,
                # The prefix is the same for every posting, so ask for it to be cached.
                system=[
                    {
                        "type": "text",
                        "text": cacheable_prefix,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
            )
        except ValidationError as e:
            raise LLMValidationError(f"response did not match {schema.__name__}: {e}") from e
        except anthropic.APIError as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMError("model refused the request")
        if resp.parsed_output is None:
            raise LLMValidationError(f"no parsed output (stop_reason={resp.stop_reason})")

        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return LLMResult(resp.parsed_output, usage, resp.model, getattr(resp, "_request_id", None))
