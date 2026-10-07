from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from jobagent.settings import get_settings
from jobagent.storage import get_storage

app = typer.Typer(help="Job search agent")
console = Console()


@app.callback()
def main() -> None:
    """Job search agent."""


@app.command("check-setup")
def check_setup() -> None:
    """Read-only smoke test: can we reach every tab? (Block 1's write test is retired: Jobs has headers now.)"""
    settings = get_settings()
    storage = get_storage(settings)
    for tab in ("Jobs", "Companies", "Runs", "Evals"):
        rows = [r for r in storage.read_rows(tab) if any(r)]
        console.print(f"[green]OK[/green] {tab}: {len(rows)} non-empty rows via {settings.storage_backend}")


@app.command("run")
def run(
    criteria_file: str = typer.Option("config/criteria.yaml", "--criteria"),
) -> None:
    """Fetch every active company, apply hard filters, dedupe, write new jobs (state: filtered)."""
    from jobagent.adapters.ats import build_adapters
    from jobagent.adapters.ats.base import make_client
    from jobagent.models.criteria import load_criteria
    from jobagent.orchestrator.pipeline import run_pipeline

    storage = get_storage(get_settings())
    with make_client() as client:
        summary = run_pipeline(
            storage, build_adapters(client), load_criteria(criteria_file), datetime.now(UTC)
        )

    table = Table(title="Run summary")
    for col in ("Company", "Fetched", "Rejected", "Duplicates", "Added", "Flags / error"):
        table.add_column(col)
    for r in summary.results:
        rejected = ", ".join(f"{k} {v}" for k, v in r.rejected.items()) or "-"
        note = r.error or ", ".join(f"{k} {v}" for k, v in r.flagged.items()) or "-"
        table.add_row(r.company, str(r.fetched), rejected, str(r.duplicates), str(r.added), note)
    console.print(table)
    if summary.skipped:
        console.print(f"Skipped (not active): {', '.join(summary.skipped)}")


@app.command("auth-drive")
def auth_drive() -> None:
    """One-time: browser consent for Drive, then create the app's root folder."""
    from jobagent.storage.drive import DriveStorage, run_consent_flow

    settings = get_settings()
    creds = run_consent_flow(settings)
    folder_id = DriveStorage(settings, credentials=creds).create_folder("Job Search Agent")
    console.print(f"[green]OK[/green] token saved to {settings.google_oauth_token_file}")
    console.print(f"Set DRIVE_FOLDER_ID={folder_id} in .env")


@app.command("check-drive")
def check_drive() -> None:
    """Probe: can we upload a file into the Drive folder (OAuth as the user)?"""
    from googleapiclient.errors import HttpError

    from jobagent.storage.drive import DriveStorage

    drive = DriveStorage(get_settings())
    try:
        file_id = drive.upload_bytes("jobagent-probe.txt", b"probe", "text/plain")
    except HttpError as e:
        console.print(f"[red]FAILED[/red] {e.status_code}: {e.reason}")
        console.print(e.content.decode(errors="replace")[:500])
        raise typer.Exit(1)
    drive.delete(file_id)
    console.print("[green]OK[/green] uploaded and deleted a test file in the Drive folder")
