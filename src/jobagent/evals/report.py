from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from jobagent.storage.base import Storage

EVALS_HEADERS = [
    "eval_id", "timestamp", "eval", "model", "prompt_version", "cases", "metric", "value",
    "target", "passed", "cost_usd", "notes",
]  # fmt: skip


@dataclass
class Metric:
    name: str
    value: float
    target: float | None = None  # None = informational, no pass/fail
    direction: Literal[">=", "<="] = ">="

    @property
    def passed(self) -> bool | None:
        if self.target is None:
            return None
        return self.value >= self.target if self.direction == ">=" else self.value <= self.target


@dataclass
class EvalReport:
    name: str  # "scorer" or "verifier"
    model: str
    prompt_version: int
    cases: int
    metrics: list[Metric]
    cost_usd: float
    failures: list[str] = field(default_factory=list)  # one readable line per miss
    notes: str = ""

    @property
    def passed(self) -> bool:
        return all(m.passed is not False for m in self.metrics)

    def to_rows(self, when: datetime) -> list[dict[str, str]]:
        eval_id = f"{when:%Y%m%dT%H%M%SZ}-{self.name}"
        return [
            {
                "eval_id": eval_id, "timestamp": when.isoformat(timespec="seconds"), "eval": self.name,
                "model": self.model, "prompt_version": str(self.prompt_version), "cases": str(self.cases),
                "metric": m.name, "value": f"{m.value:.4f}",
                "target": "" if m.target is None else f"{m.direction}{m.target:.2f}",
                "passed": "" if m.passed is None else str(m.passed).lower(),
                "cost_usd": f"{self.cost_usd:.4f}", "notes": self.notes,
            }
            for m in self.metrics
        ]  # fmt: skip

    def render(self, console) -> None:
        from rich.table import Table

        table = Table(
            title=f"{self.name} eval: {self.model}, prompt v{self.prompt_version}, {self.cases} cases"
        )
        for col in ("Metric", "Value", "Target", "Result"):
            table.add_column(col)
        for m in self.metrics:
            target = (
                ""
                if m.target is None
                else f"{m.direction} {m.target:.0%}"
                if m.target <= 1 and "rate" not in m.name
                else f"{m.direction} {m.target:.2f}"
            )
            result = (
                "" if m.passed is None else "[green]pass[/green]" if m.passed else "[red]FAIL[/red]"
            )
            value = f"{m.value:.0%}" if m.value <= 1 else f"{m.value:.2f}"
            table.add_row(m.name, value, target, result)
        console.print(table)
        for line in self.failures:
            console.print(f"  [red]miss:[/red] {line}")
        console.print(f"Cost: ${self.cost_usd:.4f}" + (f"  ({self.notes})" if self.notes else ""))


def log_report(storage: Storage, report: EvalReport, when: datetime) -> None:
    storage.append_records("Evals", EVALS_HEADERS, report.to_rows(when))
