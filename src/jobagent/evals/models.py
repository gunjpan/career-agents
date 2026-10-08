from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from jobagent.models.scoring import Level
from jobagent.models.tailoring import TailorOutput

# Ordinal scale for "off by one level" scoring.
LEVELS: list[str] = [
    "individual_contributor",
    "manager",
    "senior_manager",
    "director",
    "vp",
    "executive",
]


class ScorerCase(BaseModel):
    id: str
    company: str
    title: str
    description: str
    label_level: Level  # the TRUE level, judged from scope and not from the title
    title_matches_level: bool  # False for the traps where the title overstates or understates it


class VerifierCase(BaseModel):
    id: str
    kind: Literal["clean", "corrupt"]
    corruption: str = ""
    expected_layer: Literal["none", "code", "verifier"]  # who SHOULD stop it
    level: str
    posting: str
    draft: TailorOutput


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Targets(_Strict):
    scorer_level_accuracy: float
    scorer_within_one_level: float
    verifier_escape_rate_max: float
    verifier_false_alarm_max: float


class EvalConfig(_Strict):
    scorer_cases: str
    verifier_cases: str
    master_resume: str
    max_run_cost_usd: float
    targets: Targets


def load_eval_config(path: str | Path = "config/evals.yaml") -> EvalConfig:
    return EvalConfig.model_validate(yaml.safe_load(Path(path).read_text()))


def load_scorer_cases(path: str | Path) -> list[ScorerCase]:
    return [ScorerCase.model_validate(c) for c in yaml.safe_load(Path(path).read_text())]


def load_verifier_cases(path: str | Path) -> list[VerifierCase]:
    return [VerifierCase.model_validate(c) for c in yaml.safe_load(Path(path).read_text())]
