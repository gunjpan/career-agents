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
    """Probe: can the service account upload a file into the Drive folder?"""
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
