"""Cheap, deterministic checks on the Tailor's output. They run before the Verifier LLM:
anything code can prove wrong is sent straight back without spending a model call."""

import re
from dataclasses import dataclass

from jobagent.agents.context import CURRENT_FOCUS_ID, SUMMARY_ID
from jobagent.models.tailoring import Claim, Limits, TailoredBullet, TailoredRole, TailorOutput

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[str]:
    """Every figure in a text, normalised: '99.95%' -> '99.95', '$15MM+' -> '15', '20–25%' -> 20, 25."""
    return {n.rstrip(".,") for n in _NUMBER.findall(text)}


def squash(text: str) -> str:
    return " ".join(text.split())


@dataclass
class MasterIndex:
    bullets: dict[str, str]  # bullet id -> text (plus SUMMARY_ID -> the master summary)
    bullet_role: dict[str, str]  # bullet id -> role id
    roles: set[str]
    strengths: set[str]

    @classmethod
    def from_resume(cls, resume: dict) -> "MasterIndex":
        bullets = {SUMMARY_ID: squash(resume["summary"])}
        owner: dict[str, str] = {}
        roles: set[str] = set()
        for company in resume["experience"]:
            for role in company["roles"]:
                roles.add(role["id"])
                for b in role["bullets"]:
                    bullets[b["id"]], owner[b["id"]] = squash(b["text"]), role["id"]
        focus = resume.get("current_focus")
        if focus:
            roles.add(CURRENT_FOCUS_ID)
            for b in focus["bullets"]:
                bullets[b["id"]], owner[b["id"]] = squash(b["text"]), CURRENT_FOCUS_ID
        return cls(bullets, owner, roles, {squash(s) for s in resume["core_strengths"]})


def _check_claim(kind: str, i: int, claim: Claim, master: MasterIndex) -> list[str]:
    problems = []
    label = f"{kind} #{i}"
    if not claim.supports:
        return [f"{label} cites no master bullet ids: every claim needs support"]
    unknown = [s for s in claim.supports if s not in master.bullets]
    if unknown:
        return [f"{label} cites unknown ids {unknown}"]
    allowed = set().union(*(numbers_in(master.bullets[s]) for s in claim.supports))
    invented = numbers_in(claim.text) - allowed
    if invented:
        problems.append(f"{label} contains figures {sorted(invented)} not in the bullets it cites")
    return problems


def check_tailor_output(
    out: TailorOutput, resume: dict, limits: Limits, *, enforce_bullet_limits: bool = True
) -> list[str]:
    """Returns human-readable problems; an empty list means the output passes the cheap checks."""
    master = MasterIndex.from_resume(resume)
    problems: list[str] = []

    if len(out.summary) > limits.max_summary_sentences:
        problems.append(
            f"summary has {len(out.summary)} sentences (max {limits.max_summary_sentences})"
        )
    if len(out.cover_letter) > limits.max_cover_paragraphs:
        problems.append(
            f"cover letter has {len(out.cover_letter)} paragraphs (max {limits.max_cover_paragraphs})"
        )
    for i, c in enumerate(out.summary, 1):
        problems += _check_claim("summary sentence", i, c, master)
    for i, c in enumerate(out.cover_letter, 1):
        problems += _check_claim("cover-letter paragraph", i, c, master)

    for s in out.core_strengths:
        if squash(s) not in master.strengths:
            problems.append(f"core strength {s!r} is not in the master list (copy items verbatim)")

    seen_roles, seen_bullets, total = set(), set(), 0
    for role in out.roles:
        if role.role_id not in master.roles:
            problems.append(f"unknown role id {role.role_id!r}")
            continue
        if role.role_id in seen_roles:
            problems.append(f"role {role.role_id!r} appears twice")
        seen_roles.add(role.role_id)
        if enforce_bullet_limits and len(role.bullets) > limits.max_bullets_per_role:
            problems.append(
                f"role {role.role_id!r} has {len(role.bullets)} bullets (max {limits.max_bullets_per_role})"
            )
        for b in role.bullets:
            total += 1
            if b.source_id not in master.bullets or b.source_id == SUMMARY_ID:
                problems.append(f"unknown bullet id {b.source_id!r}")
                continue
            if master.bullet_role[b.source_id] != role.role_id:
                problems.append(
                    f"bullet {b.source_id!r} belongs to role {master.bullet_role[b.source_id]!r}, not {role.role_id!r}"
                )
            if b.source_id in seen_bullets:
                problems.append(f"bullet {b.source_id!r} used twice")
            seen_bullets.add(b.source_id)
            if not b.text.strip():
                problems.append(f"bullet {b.source_id!r} is empty")
            invented = numbers_in(b.text) - numbers_in(master.bullets[b.source_id])
            if invented:
                problems.append(
                    f"bullet {b.source_id!r} contains figures {sorted(invented)} not in the source bullet"
                )
    if enforce_bullet_limits and total > limits.max_total_bullets:
        problems.append(f"{total} bullets in total (max {limits.max_total_bullets})")
    if not out.roles:
        problems.append("no experience included")
    return problems


def complete_resume(
    out: TailorOutput, resume: dict, keep: str
) -> tuple[TailorOutput, list[str], int]:
    """Add back what the Tailor left out, copied verbatim from the master.

    keep="all_roles" adds whole roles that are missing; keep="all_bullets" also appends the missing
    bullets inside roles the Tailor did include (after the ones it chose, so its ordering and
    rewording are preserved). Verbatim copies are traceable by construction, so they need no
    Verifier pass. Returns (completed output, ids of roles added, bullets added to existing roles).
    """
    if keep == "tailor_choice":
        return out, [], 0
    master: list[tuple[str, list[dict]]] = [
        (r["id"], r["bullets"]) for c in resume["experience"] for r in c["roles"]
    ]
    if resume.get("current_focus"):
        master.append((CURRENT_FOCUS_ID, resume["current_focus"]["bullets"]))
    master_bullets = dict(master)

    def verbatim(b: dict) -> TailoredBullet:
        return TailoredBullet(source_id=b["id"], text=squash(b["text"]))

    roles, extra_bullets = [], 0
    for role in out.roles:
        if keep == "all_bullets":
            used = {b.source_id for b in role.bullets}
            missing = [
                verbatim(b) for b in master_bullets.get(role.role_id, []) if b["id"] not in used
            ]
            if missing:
                role = role.model_copy(update={"bullets": [*role.bullets, *missing]})
                extra_bullets += len(missing)
        roles.append(role)
    have = {r.role_id for r in out.roles}
    added = [rid for rid, _ in master if rid not in have]
    roles += [
        TailoredRole(role_id=rid, bullets=[verbatim(b) for b in master_bullets[rid]])
        for rid in added
    ]
    return out.model_copy(update={"roles": roles}), added, extra_bullets
