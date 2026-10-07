import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


@dataclass(frozen=True)
class Prompt:
    version: int  # logged with every score, so results trace back to the exact prompt
    text: str


def load_prompt(path: str | Path) -> Prompt:
    raw = Path(path).read_text()
    m = _FRONT_MATTER.match(raw)
    if not m:
        raise ValueError(f"{path}: prompt files need '---\\nversion: N\\n---' front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    version = meta.get("version")
    if not isinstance(version, int):
        raise TypeError(f"{path}: front matter must contain an integer 'version'")
    return Prompt(version=version, text=raw[m.end() :].strip())
