from datetime import UTC, datetime

import typer
from rich.console import Console

from jobagent.settings import get_settings
from jobagent.storage import get_storage

app = typer.Typer(help="Job search agent")
console = Console()


@app.callback()
def main() -> None:
    """Job search agent."""


@app.command("check-setup")
def check_setup() -> None:
    """Block 1 smoke test: write one row to the Jobs tab."""
    settings = get_settings()
    storage = get_storage(settings)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    storage.append_row("Jobs", ["setup-test", stamp])
    console.print(f"[green]OK[/green] wrote a row to Jobs via {settings.storage_backend}")
