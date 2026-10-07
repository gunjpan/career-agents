from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
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
        console.print(
            f"[green]OK[/green] {tab}: {len(rows)} non-empty rows via {settings.storage_backend}"
        )


@app.command("run")
def run(
    criteria_file: str = typer.Option("config/criteria.yaml", "--criteria"),
) -> None:
    """Fetch every active company, apply hard filters, dedupe, write new jobs (state: filtered)."""
    from jobagent.adapters.ats import build_adapters
    from jobagent.adapters.ats.base import make_client
    from jobagent.models.criteria import load_criteria
    from jobagent.orchestrator.pipeline import ProgressEvent, run_pipeline

    storage = get_storage(get_settings())
    criteria = load_criteria(criteria_file)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    def show(event: ProgressEvent) -> None:
        label = f"[{event.company_index}/{event.company_count}] {event.company}: {event.stage}"
        # total=None renders a pulsing bar while the count is unknown (e.g. fetching postings)
        progress.update(task, description=label, completed=event.done, total=event.total)

    # Runs in the foreground: the prompt returns only when the whole run has finished.
    with progress, make_client() as client:
        task = progress.add_task("Starting", total=None)
        summary = run_pipeline(
            storage,
            build_adapters(client, criteria.discovery),
            criteria,
            datetime.now(UTC),
            on_progress=show,
        )
        progress.update(task, description="Done", completed=1, total=1)

    table = Table(title="Run summary")
    for col in ("Company", "Fetched", "Rejected", "Duplicates", "Added", "Flags / error"):
        table.add_column(col)
    for r in summary.results:
        rejected = ", ".join(f"{k} {v}" for k, v in r.rejected.items()) or "-"
        notes = [f"{k} {v}" for k, v in r.flagged.items()]
        if r.enrich_failed:
            notes.append(f"detail failed {r.enrich_failed}")
        note = r.error or ", ".join(notes) or "-"
        table.add_row(r.company, str(r.fetched), rejected, str(r.duplicates), str(r.added), note)
    console.print(table)
    if summary.skipped:
        console.print(f"Skipped (not active): {', '.join(summary.skipped)}")


@app.command("score")
def score(
    limit: int = typer.Option(None, "--limit", "-n", help="Score at most this many jobs"),
    job_id: Annotated[
        list[str] | None, typer.Option("--job-id", help="Score only this job (repeatable)")
    ] = None,
    config_file: str = typer.Option("config/scoring.yaml", "--config"),
) -> None:
    """Score `filtered` jobs with the Level + Fit Scorer (uses the Anthropic API, costs money)."""
    from jobagent.agents.context import load_yaml, render_profiles, render_resume
    from jobagent.agents.prompts import load_prompt
    from jobagent.agents.scorer import Scorer
    from jobagent.llm.claude import ClaudeProvider
    from jobagent.models.scoring import load_scoring_config
    from jobagent.orchestrator.scoring import log_run, run_scoring

    settings = get_settings()
    if settings.anthropic_api_key is None:
        console.print("[red]ANTHROPIC_API_KEY is not set (put it in .env).[/red]")
        raise typer.Exit(1)
    config = load_scoring_config(config_file)
    prompt = load_prompt(config.prompt)
    scorer = Scorer(
        ClaudeProvider(api_key=settings.anthropic_api_key.get_secret_value()),
        config,
        prompt,
        render_resume(load_yaml("config/master_resume.yaml")),
        render_profiles(load_yaml("config/role_profiles.yaml")),
    )
    storage = get_storage(settings)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    started = datetime.now(UTC)
    with progress:
        task = progress.add_task("Starting", total=None)

        def show(done: int, total: int, title: str) -> None:
            progress.update(task, description=f"Scoring: {title[:60]}", completed=done, total=total)

        summary = run_scoring(
            storage, scorer, config, limit=limit, only=set(job_id or []) or None, on_progress=show
        )
        progress.update(task, description="Done", completed=1, total=1)
    log_run(
        storage,
        command="score",
        started_at=started,
        finished_at=datetime.now(UTC),
        summary=summary,
        config=config,
        prompt_version=prompt.version,
    )

    table = Table(title=f"Scoring summary ({config.model}, prompt v{prompt.version})")
    for col in ("Scored", "Shortlisted", "Failed", "Not attempted", "Cost (USD)"):
        table.add_column(col)
    table.add_row(
        str(summary.scored),
        str(summary.shortlisted),
        str(summary.failed),
        str(summary.remaining),
        f"${summary.cost_usd:.4f}",
    )
    console.print(table)
    console.print(
        f"Tokens: {summary.input_tokens} in, {summary.output_tokens} out, "
        f"{summary.cache_read_tokens} from cache. Levels: {dict(summary.by_level)}"
    )
    if summary.stopped:
        console.print(f"[yellow]Stopped early:[/yellow] {summary.stopped}")
    for err in summary.errors[:5]:
        console.print(f"[red]Failed:[/red] {err}")


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
