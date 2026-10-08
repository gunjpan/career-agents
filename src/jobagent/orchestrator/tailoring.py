from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from jobagent.agents.tailor import Assessment, Tailor
from jobagent.agents.verifier import Verifier, decide
from jobagent.documents.build import build_cover_letter, build_resume
from jobagent.documents.docx_writer import cover_letter_docx, resume_docx
from jobagent.documents.pdf_writer import cover_letter_pdf, resume_pdf
from jobagent.documents.report import render_report
from jobagent.llm.base import LLMError
from jobagent.models.candidates import CandidateMeta, CandidateRecord, Manifest
from jobagent.models.job import JOBS_HEADERS, Job, JobState
from jobagent.models.tailoring import TailoringConfig, TailorOutput
from jobagent.orchestrator.checks import MasterIndex, check_tailor_output, complete_resume
from jobagent.orchestrator.states import InvalidTransition, transition
from jobagent.storage.base import Storage

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ArtifactStore(Protocol):
    """Where finished documents go (Google Drive today)."""

    def create_job_folder(self, name: str) -> tuple[str, str]: ...

    def create_subfolder(self, name: str, parent_id: str) -> tuple[str, str]: ...

    def download_bytes(self, file_id: str) -> bytes: ...

    def trash(self, file_id: str) -> None: ...

    def upload_bytes(
        self, name: str, data: bytes, mime_type: str, parent_id: str | None = None
    ) -> str: ...


@dataclass
class JobOutcome:
    job_id: str
    company: str
    title: str
    status: str  # ready | blocked | error
    attempts: int = 0
    cost_usd: float = 0.0  # this run
    total_cost_usd: float = 0.0  # this job, over all runs so far
    drive_url: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class TailoringSummary:
    outcomes: list[JobOutcome] = field(default_factory=list)
    stopped: str = ""

    @property
    def cost_usd(self) -> float:
        return sum(o.cost_usd for o in self.outcomes)


def _tailor_one(job, assessment, tailor, verifier, resume, config):
    """Tailor -> cheap checks -> Verifier, with limited send-backs. Returns the final state."""
    feedback: list[str] | None = None
    history: list[str] = []
    cost, draft, verdict_out, warnings = 0.0, None, None, []
    attempts = config.max_send_backs + 1
    for n in range(1, attempts + 1):
        t = tailor.run(job, assessment, feedback)
        cost += t.cost_usd
        draft = t.output
        if not config.cover_letter and draft.cover_letter:
            draft = draft.model_copy(
                update={"cover_letter": []}
            )  # not wanted: never checked or saved
        problems = check_tailor_output(
            draft, resume, config.limits, enforce_bullet_limits=config.keep == "tailor_choice"
        )
        if problems:  # cheap code checks failed: no Verifier call, straight back to the Tailor
            history.append(f"attempt {n}: code checks failed ({len(problems)} problems)")
            feedback = problems
            continue
        v = verifier.run(job, assessment, draft)
        cost += v.cost_usd
        verdict_out, decision = v.output, decide(v.output)
        warnings = decision.warnings
        if decision.passed:
            history.append(f"attempt {n}: Verifier passed")
            return True, n, cost, draft, verdict_out, warnings, history, []
        history.append(f"attempt {n}: Verifier found {len(decision.problems)} problems")
        feedback = decision.problems
    return False, attempts, cost, draft, verdict_out, warnings, history, feedback or []


def run_tailoring(
    storage: Storage,
    tailor: Tailor,
    verifier: Verifier,
    store: ArtifactStore,
    resume: dict,
    config: TailoringConfig,
    *,
    today: date,
    only: set[str] | None = None,
    retry_blocked: bool = False,
    on_progress: Callable[[int, int, str], None] = lambda done, total, title: None,
) -> TailoringSummary:
    """approved -> tailored -> verified -> ready. A job the Verifier cannot pass stays approved."""
    storage.ensure_headers("Jobs", JOBS_HEADERS)
    rows = [
        r
        for r in storage.read_records("Jobs")
        if r["state"] == "approved"
        and (only is None or r["job_id"] in only)
        and (retry_blocked or r.get("tailor_status") != "blocked")
    ]
    summary = TailoringSummary()
    for i, rec in enumerate(rows):
        if summary.cost_usd >= config.max_run_cost_usd:
            summary.stopped = f"spend cap ${config.max_run_cost_usd:.2f} reached"
            break
        job = Job.from_record(rec)
        a = Assessment.from_record(rec)
        on_progress(i, len(rows), job.posting.title)
        outcome = JobOutcome(job.job_id, job.posting.company, job.posting.title, "error")
        summary.outcomes.append(outcome)
        manifest = Manifest.from_cell(rec.get("tailor_candidates"))
        if manifest.full:
            outcome.status = "waiting"
            outcome.notes = [
                "two candidates exist: compare with `jobagent diff`, then `jobagent pick`"
            ]
            continue
        try:
            ok, n, cost, draft, vout, warnings, history, problems = _tailor_one(
                job, a, tailor, verifier, resume, config
            )
        except LLMError as e:
            outcome.notes.append(f"model call failed: {e}")  # stays approved; retried next run
            storage.update_records(
                "Jobs",
                "job_id",
                {job.job_id: {"tailor_status": "error", "tailor_notes": str(e)[:300]}},
            )
            continue
        outcome.attempts, outcome.cost_usd = n, cost
        outcome.total_cost_usd = _prior_total(rec, manifest) + cost
        outcome.status = "ready" if ok else "blocked"
        if ok:
            draft, added_roles, added_bullets = complete_resume(draft, resume, config.keep)
            if added_roles or added_bullets:
                history.append(
                    f"added unchanged from the master: {len(added_roles)} roles"
                    f" ({', '.join(added_roles) or 'none'}) and {added_bullets} bullets in other roles"
                )

        report = render_report(
            status=outcome.status, company=job.posting.company, title=job.posting.title,
            level=a.real_level, history=history, verifier=vout, warnings=warnings, draft=draft,
            cost_usd=cost,
            versions={"Tailor prompt": f"v{tailor.prompt.version}", "Verifier prompt": f"v{verifier.prompt.version}", "Model": config.tailor.model},
        )  # fmt: skip
        if not manifest.job_folder_id:  # one folder per job; each run adds a candidate inside it
            name = config.drive_subfolder_template.format(
                company=job.posting.company, title=job.posting.title[:60], job_id=job.job_id
            )
            manifest.job_folder_id, manifest.job_folder_url = store.create_job_folder(name)

        update = {
            "tailor_status": outcome.status,
            "tailor_attempts": str(n),
            "tailor_cost_usd": f"{cost:.4f}",
            "tailor_total_cost_usd": f"{outcome.total_cost_usd:.4f}",
        }
        if ok:
            number = manifest.next_n
            folder_id, folder_url = store.create_subfolder(
                f"candidate-{number}", manifest.job_folder_id
            )
            store.upload_bytes("verifier_report.md", report.encode(), "text/markdown", folder_id)
            _upload_documents(store, folder_id, resume, draft, job, today, config.cover_letter)
            record = _candidate_record(job, draft, resume)
            json_id = store.upload_bytes("tailored.json", record.model_dump_json(indent=1).encode(), "application/json", folder_id)  # fmt: skip
            manifest.candidates.append(CandidateMeta(
                n=number, folder_id=folder_id, folder_url=folder_url, json_file_id=json_id,
                created_at=datetime.now(UTC), tailor_prompt_version=tailor.prompt.version,
                verifier_prompt_version=verifier.prompt.version, keep=config.keep, cost_usd=cost,
            ))  # fmt: skip
            outcome.drive_url = folder_url
            moved = transition(job, JobState.TAILORED)
            moved = transition(moved, JobState.VERIFIED)
            moved = transition(moved, JobState.READY)
            update |= {
                "state": moved.state.value,
                "drive_url": folder_url,
                "tailor_notes": "; ".join(warnings)[:500],
                # With one candidate there is nothing to choose; with two, the user must pick.
                "final_candidate": str(number) if len(manifest.candidates) == 1 else "",
            }
        else:
            store.upload_bytes(
                "blocked_report.md", report.encode(), "text/markdown", manifest.job_folder_id
            )
            outcome.drive_url = manifest.job_folder_url
            outcome.notes = problems[:5]
            update |= {
                "drive_url": manifest.job_folder_url,
                "tailor_notes": " | ".join(problems)[:500],
            }
        update["tailor_candidates"] = manifest.to_cell()
        storage.update_records("Jobs", "job_id", {job.job_id: update})
    return summary


def _upload_documents(
    store, folder_id, resume, draft: TailorOutput, job, today, cover_letter: bool
) -> None:
    resume_doc = build_resume(resume, draft)
    store.upload_bytes("resume.docx", resume_docx(resume_doc), DOCX, folder_id)
    store.upload_bytes("resume.pdf", resume_pdf(resume_doc), "application/pdf", folder_id)
    if cover_letter and draft.cover_letter:
        letter = build_cover_letter(
            resume, draft, company=job.posting.company, title=job.posting.title, today=today
        )
        store.upload_bytes("cover_letter.docx", cover_letter_docx(letter), DOCX, folder_id)
        store.upload_bytes(
            "cover_letter.pdf", cover_letter_pdf(letter), "application/pdf", folder_id
        )


def _prior_total(rec: dict[str, str], manifest: Manifest) -> float:
    """What this job has cost so far. Rows from before the total existed fall back to the cost of
    the candidates still on record (earlier, untracked runs can't be recovered)."""
    if rec.get("tailor_total_cost_usd"):
        return float(rec["tailor_total_cost_usd"])
    return sum(c.cost_usd for c in manifest.candidates)


def _candidate_record(job: Job, draft: TailorOutput, resume: dict) -> CandidateRecord:
    """The structured result plus the master text it was built from, so diffs stay accurate."""
    master = MasterIndex.from_resume(resume)
    used = {b.source_id for r in draft.roles for b in r.bullets} | {"summary"}
    used |= {sid for c in [*draft.summary, *draft.cover_letter] for sid in c.supports}
    labels = {
        r["id"]: f"{r['title']} ({c['company']})" for c in resume["experience"] for r in c["roles"]
    }
    if resume.get("current_focus"):
        labels["current-focus"] = f"Current focus: {resume['current_focus']['title']}"
    return CandidateRecord(
        job_id=job.job_id, company=job.posting.company, title=job.posting.title, output=draft,
        master_text={i: master.bullets[i] for i in used if i in master.bullets},
        master_strengths=sorted(master.strengths), role_labels=labels,
    )  # fmt: skip


def pick_candidate(storage: Storage, store: ArtifactStore, job_id: str, n: int) -> str:
    """You choose a version: the other candidates go to Drive's Trash and the pick becomes final."""
    rec = next((r for r in storage.read_records("Jobs") if r["job_id"] == job_id), None)
    if rec is None:
        return "not found"
    manifest = Manifest.from_cell(rec.get("tailor_candidates"))
    chosen = manifest.get(n)
    if chosen is None:
        have = ", ".join(str(c.n) for c in manifest.candidates) or "none"
        return f"not picked: no candidate {n} (candidates: {have})"
    for other in [c for c in manifest.candidates if c.n != n]:
        store.trash(other.folder_id)
    manifest.candidates = [chosen]
    storage.update_records("Jobs", "job_id", {job_id: {
        "tailor_candidates": manifest.to_cell(), "final_candidate": str(n), "drive_url": chosen.folder_url,
    }})  # fmt: skip
    return f"picked candidate {n}: {rec['company']} / {rec['title']}"


def approve(storage: Storage, job_ids: list[str]) -> dict[str, str]:
    """The human gate: shortlisted -> approved. Returns {job_id: result}."""
    results: dict[str, str] = {}
    by_id = {r["job_id"]: r for r in storage.read_records("Jobs")}
    updates: dict[str, dict[str, str]] = {}
    for job_id in job_ids:
        rec = by_id.get(job_id)
        if rec is None:
            results[job_id] = "not found"
            continue
        if (
            rec["state"] != "shortlisted"
        ):  # the state machine also allows ready -> approved, for retailor
            results[job_id] = (
                f"not approved: state is {rec['state']}, only shortlisted jobs can be approved"
            )
            continue
        try:
            approved = transition(Job.from_record(rec), JobState.APPROVED, actor="human")
        except InvalidTransition as e:  # only shortlisted jobs can be approved
            results[job_id] = f"not approved: {e}"
            continue
        updates[job_id] = {"state": approved.state.value}
        results[job_id] = f"approved: {rec['company']} / {rec['title']}"
    if updates:
        storage.update_records("Jobs", "job_id", updates)
    return results


# Candidates and the Drive link are kept: the previous version stays until you pick between them.
RETAILOR_RESET = {
    "tailor_status": "",
    "tailor_notes": "",
    "tailor_attempts": "",
    "tailor_cost_usd": "",
}


def retailor(storage: Storage, job_ids: list[str]) -> dict[str, str]:
    """The human gate for a second try: send a tailored (or blocked) job back to be tailored again.

    `ready` -> `approved` is a human-only move. A `blocked` or `error` job is already `approved`, so
    only its tailoring columns are cleared. Anything not yet tailored, or already submitted, is refused.
    """
    results: dict[str, str] = {}
    by_id = {r["job_id"]: r for r in storage.read_records("Jobs")}
    updates: dict[str, dict[str, str]] = {}
    for job_id in job_ids:
        rec = by_id.get(job_id)
        if rec is None:
            results[job_id] = "not found"
        elif rec["state"] == "approved" and rec.get("tailor_status") in {"blocked", "error"}:
            updates[job_id] = dict(RETAILOR_RESET)
            results[job_id] = (
                f"reset: {rec['company']} / {rec['title']} (was {rec['tailor_status']})"
            )
        elif rec["state"] == "ready" and Manifest.from_cell(rec.get("tailor_candidates")).full:
            results[job_id] = (
                "not changed: two candidates already exist, pick one first (jobagent diff, jobagent pick)"
            )
        elif rec["state"] == "ready":
            moved = transition(Job.from_record(rec), JobState.APPROVED, actor="human")
            updates[job_id] = {"state": moved.state.value, **RETAILOR_RESET}
            results[job_id] = f"approved again: {rec['company']} / {rec['title']}"
        else:
            results[job_id] = (
                f"not changed: state is {rec['state']}, only ready or blocked jobs can be re-tailored"
            )
    if updates:
        storage.update_records("Jobs", "job_id", updates)
    return results
