"""The `jobagent demo` walkthrough: the REAL pipeline on fictional data, with replayed model answers."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jobagent.agents.context import load_yaml, render_profiles, render_resume
from jobagent.agents.prompts import load_prompt
from jobagent.agents.scorer import Scorer
from jobagent.agents.tailor import Tailor
from jobagent.agents.verifier import Verifier
from jobagent.demo.store import LocalArtifactStore
from jobagent.documents.diff import build_diff, diff_text, word_diff
from jobagent.evals.models import LEVELS, ScorerCase, load_scorer_cases
from jobagent.llm.base import LLMProvider
from jobagent.models.candidates import CandidateRecord, Manifest
from jobagent.models.company import Company
from jobagent.models.criteria import EXAMPLE_CRITERIA, load_criteria
from jobagent.models.job import RawPosting
from jobagent.models.scoring import load_scoring_config
from jobagent.models.tailoring import load_tailoring_config
from jobagent.orchestrator.checks import check_tailor_output
from jobagent.orchestrator.onboarding import add_company
from jobagent.orchestrator.pipeline import run_pipeline
from jobagent.orchestrator.scoring import run_scoring
from jobagent.orchestrator.tailoring import approve, run_tailoring
from jobagent.storage.csv_store import CsvStorage

# Fictional postings from the eval set. Mixed on purpose: two are filtered out by the hard rules.
DEMO_CASES = [
    "s1-senior-manager-clear", "s2-director-title-senior-manager-scope", "s3-avp-title-first-line-manager",
    "s4-principal-engineer-individual-contributor", "d1-director-clear", "v2-vp-clear",
]  # fmt: skip
EXAMPLE_RESUME = "config/master_resume.example.yaml"


class DemoBoard:
    """A stand-in job board (the ATSAdapter interface) serving the fictional postings."""

    name = "demo"

    def __init__(self, cases: list[ScorerCase], now: datetime) -> None:
        self.by_company, self.now = {c.company: (i, c) for i, c in enumerate(cases)}, now

    def fetch(self, company: Company) -> list[RawPosting]:
        i, case = self.by_company[company.name]
        return [RawPosting(
            company=case.company, ats="demo", external_id=case.id, title=case.title, locations=["Toronto, ON"],
            url=f"https://example.invalid/jobs/{case.id}", posted_at=self.now - timedelta(days=2 + i),
            department="Engineering", description=case.description,
        )]  # fmt: skip


@dataclass
class DemoResult:
    companies: int = 0
    fetched: int = 0
    filtered_out: int = 0
    scored: int = 0
    shortlisted: int = 0
    approved: str = ""
    tailor_status: str = ""
    attempts: int = 0
    claims_checked: int = 0
    files: list[str] = field(default_factory=list)
    fabrication_caught: bool = False
    cost_usd: float = 0.0
    model_calls: int = 0


def _step(console: Console, n: int, title: str, pause: Callable[[float], None]) -> None:
    pause(0.8)
    console.print()
    console.rule(f"[bold cyan]{n}. {title}")
    pause(0.4)


def run_demo(
    console: Console,
    provider: LLMProvider,
    workdir: Path,
    *,
    pause: Callable[[float], None] = lambda s: None,
    now: datetime | None = None,
    recording_note: str = "",
) -> DemoResult:
    now = now or datetime.now(UTC)
    result = DemoResult()
    workdir = Path(workdir)
    storage = CsvStorage(str(workdir / "data"))
    store = LocalArtifactStore(workdir / "output")
    criteria = load_criteria(EXAMPLE_CRITERIA)  # the generic public rules, never a private file
    scoring, tcfg = load_scoring_config(), load_tailoring_config()
    cases = {c.id: c for c in load_scorer_cases("evals/data/scorer_cases.yaml")}
    chosen = [cases[i] for i in DEMO_CASES]
    resume = load_yaml(EXAMPLE_RESUME)
    profiles = load_yaml("config/role_profiles.yaml")

    console.print(Panel.fit(
        "[bold]jobagent demo[/bold]  ·  fictional companies and postings, real pipeline code\n"
        f"Model answers are [bold]recorded[/bold] from real runs{recording_note}: no network, no API keys, "
        "nothing leaves this folder.\n[dim]Where a step is yours (approving), the demo says so.[/dim]",
        border_style="cyan",
    ))  # fmt: skip

    # 1. Companies ------------------------------------------------------------------------
    _step(console, 1, "Companies: adding one is one row", pause)
    for c in chosen:
        add_company(storage, c.company, "A", ["senior_manager", "director", "vp"])
        storage.update_records(
            "Companies", "name", {c.company: {"ats": "demo", "board_id": c.id, "status": "active"}}
        )
    result.companies = len(chosen)
    console.print(f"{len(chosen)} fictional companies added. [dim]In real use: `jobagent add-company NAME`, and the "
                  "Onboarding agent finds each company's job platform and checks it with a real fetch.[/dim]")  # fmt: skip

    # 2. Fetch and filter ------------------------------------------------------------------
    _step(console, 2, "Fetch and filter: hard rules run before any model is called", pause)
    summary = run_pipeline(storage, {"demo": DemoBoard(chosen, now)}, criteria, now)
    table = Table("Company", "Posting", "Outcome")
    rejected = {r.company: r.rejected for r in summary.results}
    for c in chosen:
        why = ", ".join(rejected[c.company]) if rejected[c.company] else ""
        table.add_row(
            c.company, c.title, f"[red]filtered out[/red] ({why})" if why else "[green]kept[/green]"
        )
    console.print(table)
    result.fetched = sum(r.fetched for r in summary.results)
    result.filtered_out = sum(sum(r.rejected.values()) for r in summary.results)
    console.print(f"{result.fetched} postings fetched, {result.filtered_out} filtered out by rules, "
                  f"{result.fetched - result.filtered_out} kept. [dim]Cost so far: $0.00, no model involved.[/dim]")  # fmt: skip

    # 3. Score ------------------------------------------------------------------------------
    _step(console, 3, "Score: the Scorer judges level and fit; code decides the shortlist", pause)
    scorer = Scorer(
        provider,
        scoring,
        load_prompt(scoring.prompt),
        render_resume(resume),
        render_profiles(profiles),
    )
    scored = run_scoring(storage, scorer, scoring)
    rows = [r for r in storage.read_records("Jobs") if r["state"] in {"scored", "shortlisted"}]
    table = Table("Posting title", "Level (model)", "Fit", "Core domain", "Result")
    for r in sorted(rows, key=lambda r: -int(r["fit_score"])):
        result_label = "[green]shortlisted[/green]" if r["state"] == "shortlisted" else "scored"
        table.add_row(
            r["title"],
            f"{r['real_level']} ({r['level_confidence']})",
            r["fit_score"],
            r["core_domain_covered"],
            result_label,
        )
    console.print(table)
    result.scored, result.shortlisted = scored.scored, scored.shortlisted
    result.cost_usd += scored.cost_usd
    console.print(f"[dim]Shortlist rule, in code: fit >= {scoring.shortlist.min_fit}, level in {scoring.shortlist.levels}, "
                  f"core domain covered. Recorded cost: ${scored.cost_usd:.4f}[/dim]")  # fmt: skip

    # 4. Human gate --------------------------------------------------------------------------
    _step(console, 4, "Your decision: only a human can approve a job", pause)
    # Highest fit first; among equals, the more senior role (deterministic, so the recording stays valid).
    shortlisted = sorted(
        (r for r in rows if r["state"] == "shortlisted"),
        key=lambda r: (-int(r["fit_score"]), -LEVELS.index(r["real_level"])),
    )
    if not shortlisted:
        console.print(
            "[yellow]Nothing was shortlisted in this recording, so the demo stops here.[/yellow]"
        )
        result.model_calls = getattr(provider, "calls", 0)
        return result
    top = shortlisted[0]
    console.print(f"[bold]You[/bold] review the shortlist and approve one:  [cyan]jobagent approve {top['job_id']}[/cyan]  "
                  f"({top['title']})\n[dim](The demo plays your part. The pipeline itself can never do this step.)[/dim]")  # fmt: skip
    console.print(approve(storage, [top["job_id"]])[top["job_id"]])
    result.approved = top["job_id"]

    # 5. Tailor and verify --------------------------------------------------------------------
    _step(
        console,
        5,
        "Tailor and verify: reword from your master resume, then check every claim",
        pause,
    )
    ids_text = render_resume(resume, include_ids=True)
    tailor = Tailor(
        provider,
        tcfg,
        load_prompt(tcfg.tailor.prompt),
        ids_text,
        profiles,
        scoring.pricing_usd_per_mtok[tcfg.tailor.model],
    )
    verifier = Verifier(
        provider,
        tcfg,
        load_prompt(tcfg.verifier.prompt),
        ids_text,
        scoring.pricing_usd_per_mtok[tcfg.verifier.model],
    )
    tailored = run_tailoring(storage, tailor, verifier, store, resume, tcfg, today=now.date())
    outcome = tailored.outcomes[0]
    result.tailor_status, result.attempts = outcome.status, outcome.attempts
    result.cost_usd += outcome.cost_usd
    row = next(r for r in storage.read_records("Jobs") if r["job_id"] == top["job_id"])
    manifest = Manifest.from_cell(row["tailor_candidates"])
    candidate = manifest.candidates[-1]
    report = next(Path(candidate.folder_id).glob("verifier_report.md")).read_text()
    verdict_rows = ("| traced |", "| inflated |", "| unsupported |", "| wrong_level |")
    result.claims_checked = sum(line.startswith(verdict_rows) for line in report.splitlines())
    result.files = sorted(p.name for p in Path(candidate.folder_id).iterdir())
    colour = "green" if outcome.status == "ready" else "red"
    console.print(f"Status: [{colour}]{outcome.status}[/{colour}] after {outcome.attempts} attempt(s). "
                  f"The Verifier checked {result.claims_checked} claims; every one must trace to a master bullet.")  # fmt: skip
    console.print(f"Saved: {', '.join(result.files)}  [dim]({candidate.folder_id})[/dim]")
    console.print(
        "[dim]Pass or fail is decided in code from the Verifier's verdicts, not by the model.[/dim]"
    )

    # 6. What changed ---------------------------------------------------------------------------
    _step(console, 6, "Diff: what the Tailor changed versus your master resume", pause)
    record = CandidateRecord.model_validate_json(store.download_bytes(candidate.json_file_id))
    view = build_diff({candidate.n: record})
    from rich.text import Text

    n = candidate.n
    reworded = [(r, b) for r in view.roles for b in r.bullets if b.status[n] == "reworded"]
    kept = sum(1 for r in view.roles for b in r.bullets if b.status[n] == "unchanged")
    console.print("[bold]Summary[/bold] [dim](green = added, red = removed)[/dim]")
    console.print(Text("  ") + diff_text(word_diff(view.master_summary, view.summaries[n])))
    chosen_strengths = [s for s in view.strengths[n] if s in view.master_strengths]
    console.print(f"[bold]Core strengths:[/bold] {len(chosen_strengths)} of {len(view.master_strengths)} chosen, all copied from your master list")  # fmt: skip
    console.print(f"[bold]Experience:[/bold] {len(reworded)} bullets reworded, {kept} copied unchanged. "
                  "[dim]The Tailor is told to copy a bullet exactly when rewording would not help.[/dim]")  # fmt: skip
    for _, b in reworded[:3]:
        console.print(
            Text(f"  [{b.source_id}] ") + diff_text(word_diff(b.master, b.texts[n] or ""))
        )

    # 7. Safety: an injected fabrication ----------------------------------------------------------
    _step(console, 7, "Safety: what if the model invented something?", pause)
    first_role = record.output.roles[0]
    forged = record.output.model_copy(deep=True)
    forged.roles[0].bullets[0].text += " across 40 markets"
    problems = check_tailor_output(forged, resume, tcfg.limits, enforce_bullet_limits=False)
    console.print(f"[bold]Injected on purpose:[/bold] bullet [cyan]{first_role.bullets[0].source_id}[/cyan] now says "
                  f"\"... across 40 markets\" (not in your master).")  # fmt: skip
    if problems:
        console.print(f"[green]Stopped by plain code, before any model call:[/green] {problems[0]}")
        result.fabrication_caught = True
    else:
        console.print("[red]Not caught by the code checks.[/red]")

    # 8. Wrap up ----------------------------------------------------------------------------------
    _step(console, 8, "Result", pause)
    result.model_calls = getattr(provider, "calls", 0)
    wrap = Table(show_header=True, header_style="bold", title="What the pipeline did")
    wrap.add_column("Step")
    wrap.add_column("Result")
    wrap.add_row("Postings seen", str(result.fetched))
    wrap.add_row("Kept after hard filters", str(result.fetched - result.filtered_out))
    wrap.add_row("Scored", str(result.scored))
    wrap.add_row("Shortlisted", str(result.shortlisted))
    wrap.add_row(
        "Resume tailored and verified",
        f"{result.tailor_status} ({result.claims_checked} claims traced)",
    )
    wrap.add_row("Model calls replayed", str(result.model_calls))
    wrap.add_row("Recorded cost", f"${result.cost_usd:.4f}")
    wrap.add_row("Submitted anywhere", "[bold]nothing[/bold]: a human submits, never the pipeline")
    console.print(wrap)
    return result
