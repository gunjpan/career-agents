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


def _build_onboarder(settings, client, criteria):
    """The Onboarder, or None (with a message) when there is no Gemini key."""
    from jobagent.adapters.ats import build_adapters
    from jobagent.agents.prompts import load_prompt
    from jobagent.llm.gemini import GeminiProvider
    from jobagent.models.onboarding import load_onboarding_config
    from jobagent.models.scoring import load_scoring_config
    from jobagent.onboarding.onboard import Onboarder

    if settings.gemini_api_key is None:
        console.print(
            "[yellow]GEMINI_API_KEY is not set, so pending companies are not onboarded.[/yellow]"
        )
        return None
    config = load_onboarding_config()
    pricing = None if config.free_tier else load_scoring_config().pricing_usd_per_mtok[config.model]
    provider = GeminiProvider(
        settings.gemini_api_key.get_secret_value(), free_tier=config.free_tier
    )
    return Onboarder(provider, config, load_prompt(config.prompt), build_adapters(client, criteria.discovery), client, pricing)  # fmt: skip


def _build_scorer(settings, config_file: str = "config/scoring.yaml"):
    """(Scorer, ScoringConfig, Prompt) for the Claude-based Level and Fit Scorer."""
    from jobagent.agents.context import (
        load_master_resume,
        load_yaml,
        render_profiles,
        render_resume,
    )
    from jobagent.agents.prompts import load_prompt
    from jobagent.agents.scorer import Scorer
    from jobagent.llm.claude import ClaudeProvider
    from jobagent.models.scoring import load_scoring_config

    config = load_scoring_config(config_file)
    prompt = load_prompt(config.prompt)
    scorer = Scorer(
        ClaudeProvider(api_key=settings.anthropic_api_key.get_secret_value()),
        config, prompt, render_resume(load_master_resume(settings)),
        render_profiles(load_yaml("config/role_profiles.yaml")),
    )  # fmt: skip
    return scorer, config, prompt


def _print_onboarding(results, onboarder) -> None:
    for company, r in results:
        colour = {"active": "green", "needs_review": "yellow", "pending": "red"}[r.status]
        target = f" -> {r.ats}/{r.board_id}" if r.ats else ""
        console.print(
            f"[{colour}]{r.status:12}[/{colour}] {company.name}{target}\n             {r.reason}"
        )
    t = onboarder.tokens
    console.print(
        f"[dim]Onboarding model: {t['input']} tokens in, {t['output']} out, ${onboarder.cost_usd:.4f}[/dim]"
    )


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
    force: Annotated[bool, typer.Option("--force", help="Ignore the fetch cooldown")] = False,
    company: Annotated[
        list[str] | None, typer.Option("--company", help="Only this company (repeatable)")
    ] = None,
) -> None:
    """Fetch every active company, apply hard filters, dedupe, write new jobs (state: filtered)."""
    from jobagent.adapters.ats import build_adapters
    from jobagent.adapters.ats.base import make_client
    from jobagent.models.criteria import load_criteria
    from jobagent.models.fetch_policy import load_fetch_policy
    from jobagent.orchestrator.pipeline import ProgressEvent, run_pipeline

    settings = get_settings()
    storage = get_storage(settings)
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
        if any(r.get("status") == "pending" for r in storage.read_records("Companies")):
            from jobagent.orchestrator.onboarding import run_onboarding

            progress.update(task, description="Onboarding new companies")
            onboarder = _build_onboarder(settings, client, criteria)
            if onboarder is not None:
                results = run_onboarding(storage, onboarder)
                progress.stop()
                _print_onboarding(results, onboarder)
                progress.start()
        summary = run_pipeline(
            storage,
            build_adapters(client, criteria.discovery),
            criteria,
            datetime.now(UTC),
            on_progress=show,
            policy=load_fetch_policy(),
            force=force,
            only={n.lower() for n in company} if company else None,
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
        note = r.error or r.cooldown or ", ".join(notes) or "-"
        table.add_row(r.company, str(r.fetched), rejected, str(r.duplicates), str(r.added), note)
    console.print(table)
    if summary.skipped:
        console.print(f"Skipped (not active): {', '.join(summary.skipped)}")


@app.command("add-company")
def add_company_cmd(
    name: Annotated[str, typer.Argument(help='e.g. "Wealthsimple"')],
    tier: Annotated[str, typer.Option(help="Priority tier, e.g. A")] = "A",
    levels: Annotated[
        str, typer.Option(help="Comma-separated target levels")
    ] = "senior_manager,director,vp",
    onboard_now: Annotated[
        bool, typer.Option("--onboard", help="Find its job platform right away")
    ] = False,
) -> None:
    """Add a company as one `pending` row; the next `jobagent run` finds its job platform."""
    from jobagent.orchestrator.onboarding import add_company

    storage = get_storage(get_settings())
    console.print(
        add_company(storage, name, tier, [x.strip() for x in levels.split(",") if x.strip()])
    )
    if onboard_now:
        onboard_cmd([name], dry_run=False)


@app.command("onboard")
def onboard_cmd(
    names: Annotated[
        list[str] | None, typer.Argument(help="Company names; default: every pending company")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the result without writing to the Sheet")
    ] = False,
) -> None:
    """Find each company's job platform and board id, validated by fetching real postings."""
    from jobagent.adapters.ats.base import make_client
    from jobagent.models.criteria import load_criteria
    from jobagent.orchestrator.onboarding import run_onboarding

    settings = get_settings()
    with make_client() as client:
        onboarder = _build_onboarder(settings, client, load_criteria())
        if onboarder is None:
            raise typer.Exit(1)
        if dry_run and names:  # try names that are not in the Sheet at all
            from jobagent.models.company import Company

            results = [(c, onboarder.onboard(c)) for c in (Company(name=n) for n in names)]
        else:
            results = run_onboarding(
                get_storage(settings),
                onboarder,
                names=set(names) if names else None,
                dry_run=dry_run,
            )
        _print_onboarding(results, onboarder)
        if not results:
            console.print("Nothing to onboard: no pending companies.")


@app.command("daily")
def daily(
    no_score: Annotated[
        bool, typer.Option("--no-score", help="Fetch and filter only (free)")
    ] = False,
) -> None:
    """The scheduled job: onboard new companies, fetch and filter, score. Safe for a public log."""
    import os
    from datetime import UTC, datetime
    from pathlib import Path

    from jobagent.adapters.ats import build_adapters
    from jobagent.adapters.ats.base import make_client
    from jobagent.models.criteria import load_criteria
    from jobagent.models.fetch_policy import load_fetch_policy
    from jobagent.orchestrator.daily import run_daily

    settings = get_settings()
    storage = get_storage(settings)
    criteria = load_criteria()
    scorer = scoring_config = prompt = None
    if not no_score and settings.anthropic_api_key is not None:
        scorer, scoring_config, prompt = _build_scorer(settings)
    with make_client() as client:
        onboarder = _build_onboarder(settings, client, criteria)
        summary = run_daily(
            storage, build_adapters(client, criteria.discovery), criteria, load_fetch_policy(),
            now=datetime.now(UTC), clock=lambda: datetime.now(UTC), onboarder=onboarder, scorer=scorer,
            scoring_config=scoring_config, prompt_version=prompt.version if prompt else 0,
        )  # fmt: skip
    markdown = summary.to_markdown()
    console.print(markdown, markup=False)
    if os.environ.get("GITHUB_STEP_SUMMARY"):  # the summary page on the Actions run
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as f:
            f.write(markdown)
    raise typer.Exit(summary.exit_code)


@app.command("score")
def score(
    limit: int = typer.Option(None, "--limit", "-n", help="Score at most this many jobs"),
    job_id: Annotated[
        list[str] | None, typer.Option("--job-id", help="Score only this job (repeatable)")
    ] = None,
    config_file: str = typer.Option("config/scoring.yaml", "--config"),
) -> None:
    """Score `filtered` jobs with the Level + Fit Scorer (uses the Anthropic API, costs money)."""
    from jobagent.orchestrator.scoring import log_run, run_scoring

    settings = get_settings()
    if settings.anthropic_api_key is None:
        console.print("[red]ANTHROPIC_API_KEY is not set (put it in .env).[/red]")
        raise typer.Exit(1)
    scorer, config, prompt = _build_scorer(settings, config_file)
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


@app.command("shortlist")
def shortlist() -> None:
    """Show shortlisted and approved jobs, best fit first. Approve with `jobagent approve`."""
    rows = [r for r in get_storage(get_settings()).read_records("Jobs")
            if r["state"] in {"shortlisted", "approved", "ready"}]  # fmt: skip
    rows.sort(key=lambda r: int(r.get("fit_score") or 0), reverse=True)
    console.print(f"[bold]{len(rows)} jobs, best fit first[/bold]  (id  fit  level  state)")
    for r in rows:
        tailor_state = f" | tailor: {r['tailor_status']}" if r.get("tailor_status") else ""
        console.print(
            f"[cyan]{r['job_id']}[/cyan]  {r.get('fit_score', ''):>3}  {r.get('real_level', ''):<14}"
            f" {r['state']:<11} {r['company']}: {r['title']}{tailor_state}",
            soft_wrap=True,
        )


@app.command("approve")
def approve_cmd(
    job_ids: Annotated[list[str], typer.Argument(help="job_id values from `shortlist`")],
) -> None:
    """You approve jobs for tailoring. Only shortlisted jobs can be approved, and only you can."""
    from jobagent.orchestrator.tailoring import approve

    for job_id, result in approve(get_storage(get_settings()), job_ids).items():
        colour = "green" if result.startswith("approved") else "red"
        console.print(f"[{colour}]{job_id}[/{colour}] {result}")


@app.command("retailor")
def retailor_cmd(
    job_ids: Annotated[list[str], typer.Argument(help="job_id values to re-tailor")],
) -> None:
    """You send a tailored or blocked job back for another go; then run `jobagent tailor`."""
    from jobagent.orchestrator.tailoring import retailor

    for job_id, result in retailor(get_storage(get_settings()), job_ids).items():
        colour = "green" if result.startswith(("reset", "approved")) else "red"
        console.print(f"[{colour}]{job_id}[/{colour}] {result}")


@app.command("diff")
def diff_cmd(job_id: Annotated[str, typer.Argument(help="job_id from `shortlist`")]) -> None:
    """Compare the master resume with a job's candidate version(s), word by word."""
    from pathlib import Path

    from jobagent.documents.diff import build_diff, render_html, render_terminal
    from jobagent.models.candidates import CandidateRecord, Manifest
    from jobagent.storage.drive import DriveStorage

    settings = get_settings()
    rec = next(
        (r for r in get_storage(settings).read_records("Jobs") if r["job_id"] == job_id), None
    )
    manifest = Manifest.from_cell(rec.get("tailor_candidates") if rec else None)
    if rec is None or not manifest.candidates:
        console.print(
            "[red]No tailored candidates for that job. Run `jobagent tailor` first.[/red]"
        )
        raise typer.Exit(1)
    drive = DriveStorage(settings)
    records = {
        c.n: CandidateRecord.model_validate_json(drive.download_bytes(c.json_file_id))
        for c in manifest.candidates
    }
    view = build_diff(records)
    render_terminal(view, console)
    out = Path("output/diffs") / f"{job_id}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(view))
    console.print(f"\nSide-by-side view: [cyan]{out}[/cyan] (open it in a browser)")
    if len(manifest.candidates) > 1:
        console.print(
            f"Choose with: jobagent pick {job_id} {' or '.join(str(c.n) for c in manifest.candidates)}"
        )


@app.command("pick")
def pick_cmd(
    job_id: Annotated[str, typer.Argument(help="job_id from `shortlist`")],
    candidate: Annotated[int, typer.Argument(help="the candidate number to keep")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation")] = False,
) -> None:
    """Keep one candidate as the final resume; the others go to Drive's Trash (recoverable)."""
    from jobagent.models.candidates import Manifest
    from jobagent.orchestrator.tailoring import pick_candidate
    from jobagent.storage.drive import DriveStorage

    settings = get_settings()
    storage = get_storage(settings)
    rec = next((r for r in storage.read_records("Jobs") if r["job_id"] == job_id), None)
    manifest = Manifest.from_cell(rec.get("tailor_candidates") if rec else None)
    losers = [c.n for c in manifest.candidates if c.n != candidate]
    if losers and not yes:
        typer.confirm(
            f"Keep candidate {candidate} and move candidate(s) {losers} to Drive's Trash?",
            abort=True,
        )
    result = pick_candidate(storage, DriveStorage(settings), job_id, candidate)
    console.print(f"[{'green' if result.startswith('picked') else 'red'}]{result}[/]")


@app.command("tailor")
def tailor(
    job_id: Annotated[
        list[str] | None, typer.Option("--job-id", help="Only this job (repeatable)")
    ] = None,
    retry_blocked: Annotated[bool, typer.Option("--retry", help="Also retry blocked jobs")] = False,
    cover_letter: Annotated[
        bool, typer.Option("--cover-letter", help="Also write a cover letter")
    ] = False,
) -> None:
    """Tailor + verify approved jobs, then save resume, cover letter and report to Drive."""
    from jobagent.agents.context import load_master_resume, load_yaml, render_resume
    from jobagent.agents.prompts import load_prompt
    from jobagent.agents.tailor import Tailor
    from jobagent.agents.verifier import Verifier
    from jobagent.llm.claude import ClaudeProvider
    from jobagent.models.scoring import load_scoring_config
    from jobagent.models.tailoring import load_tailoring_config
    from jobagent.orchestrator.tailoring import run_tailoring
    from jobagent.storage.drive import DriveStorage

    settings = get_settings()
    if settings.anthropic_api_key is None:
        console.print("[red]ANTHROPIC_API_KEY is not set (put it in .env).[/red]")
        raise typer.Exit(1)
    config = load_tailoring_config()
    if cover_letter:  # per-run override of tailoring.yaml
        config = config.model_copy(update={"cover_letter": True})
    scoring = load_scoring_config()
    master = load_master_resume(settings)
    text = render_resume(master, include_ids=True)
    provider = ClaudeProvider(api_key=settings.anthropic_api_key.get_secret_value())
    t_prompt, v_prompt = load_prompt(config.tailor.prompt), load_prompt(config.verifier.prompt)
    tailor_agent = Tailor(provider, config, t_prompt, text, load_yaml("config/role_profiles.yaml"),
                          scoring.pricing_usd_per_mtok[config.tailor.model])  # fmt: skip
    verifier_agent = Verifier(
        provider, config, v_prompt, text, scoring.pricing_usd_per_mtok[config.verifier.model]
    )

    progress = Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                        TimeElapsedColumn(), console=console)  # fmt: skip
    with progress:
        task = progress.add_task("Starting", total=None)

        def show(done: int, total: int, title: str) -> None:
            progress.update(
                task, description=f"Tailoring: {title[:55]}", completed=done, total=total
            )

        summary = run_tailoring(
            get_storage(settings), tailor_agent, verifier_agent, DriveStorage(settings), master, config,
            today=datetime.now(UTC).date(), only=set(job_id or []) or None, retry_blocked=retry_blocked, on_progress=show,
        )  # fmt: skip
        progress.update(task, description="Done", completed=1, total=1)

    table = Table(title="Tailoring summary")
    for col in (
        "Company",
        "Title",
        "Status",
        "Attempts",
        "This run",
        "Job total",
        "Folder / notes",
    ):
        table.add_column(col, overflow="fold")
    for o in summary.outcomes:
        table.add_row(o.company, o.title[:40], o.status, str(o.attempts), f"${o.cost_usd:.3f}", f"${o.total_cost_usd:.3f}",
                      o.drive_url or "; ".join(o.notes)[:120])  # fmt: skip
    console.print(table)
    console.print(f"Total cost: ${summary.cost_usd:.3f}")
    if summary.stopped:
        console.print(f"[yellow]Stopped early:[/yellow] {summary.stopped}")


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
