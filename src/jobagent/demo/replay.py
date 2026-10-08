"""Record real model responses once, replay them forever: a free, offline, honest demo.

The demo runs the real pipeline code; only the model call is replaced. Each response is stored under
a hash of everything the model was sent (schema, model, instructions and prompt), so if a prompt or
profile changes later the stored answer no longer matches and the demo says so, instead of silently
showing output that the current code would not produce.
"""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from jobagent.llm.base import LLMProvider, LLMResult, Usage

RECORDING = Path(__file__).with_name("recorded_responses.json")


class ReplayMiss(Exception):
    """No recorded response for this exact request. Not an LLMError, so no agent swallows it."""


def request_key(schema, model: str, prefix: str, prompt: str) -> str:
    blob = f"{schema.__name__}\x1f{model}\x1f{prefix}\x1f{prompt}"
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


class ReplayProvider:
    """An LLMProvider that answers from a recording. Works with every agent (no data restriction)."""

    public_data_only = False

    def __init__(self, path: Path = RECORDING) -> None:
        data = json.loads(Path(path).read_text())
        self.meta = data.get("meta", {})
        self.entries: dict[str, dict] = data["responses"]
        self.calls = 0

    def complete(
        self, *, model, cacheable_prefix, prompt, schema, max_tokens, temperature=0.0, effort=None
    ) -> LLMResult:
        key = request_key(schema, model, cacheable_prefix, prompt)
        entry = self.entries.get(key)
        if entry is None:
            raise ReplayMiss(
                f"no recorded {schema.__name__} response for this request (key {key}). A prompt, profile or "
                "input changed since the demo was recorded: run `jobagent demo --record` to refresh it."
            )
        self.calls += 1
        return LLMResult(
            schema.model_validate(entry["output"]), Usage(**entry["usage"]), entry["model"]
        )


class RecordingProvider:
    """Wraps a real provider and saves every response, for `jobagent demo --record`."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.public_data_only = getattr(inner, "public_data_only", False)
        self.entries: dict[str, dict] = {}

    def complete(
        self, *, model, cacheable_prefix, prompt, schema, max_tokens, temperature=0.0, effort=None
    ) -> LLMResult:
        result = self.inner.complete(model=model, cacheable_prefix=cacheable_prefix, prompt=prompt, schema=schema,
                                     max_tokens=max_tokens, temperature=temperature, effort=effort)  # fmt: skip
        self.entries[request_key(schema, model, cacheable_prefix, prompt)] = {
            "schema": schema.__name__, "model": result.model,
            "usage": asdict(result.usage), "output": result.parsed.model_dump(mode="json"),
        }  # fmt: skip
        return result

    def save(self, path: Path = RECORDING) -> int:
        meta = {"recorded_at": datetime.now(UTC).isoformat(timespec="seconds"), "responses": len(self.entries),
                "models": sorted({e["model"] for e in self.entries.values()})}  # fmt: skip
        Path(path).write_text(
            json.dumps({"meta": meta, "responses": self.entries}, indent=1, sort_keys=True)
        )
        return len(self.entries)
