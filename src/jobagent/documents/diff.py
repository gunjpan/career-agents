"""Compare the master resume with one or two tailored candidates, bullet by bullet.

Bullets are matched by their master id, so a bullet that moved is still compared with its source.
"""

import difflib
from dataclasses import dataclass, field
from html import escape
from typing import Literal

from rich.text import Text

from jobagent.models.candidates import CandidateRecord

Op = Literal["same", "add", "del"]
Status = Literal["unchanged", "reworded", "absent"]


@dataclass
class Piece:
    op: Op
    text: str


def word_diff(old: str, new: str) -> list[Piece]:
    """Word-level diff: what was removed from `old` and what was added to make `new`."""
    a, b = old.split(), new.split()
    pieces: list[Piece] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            pieces.append(Piece("same", " ".join(a[i1:i2])))
            continue
        if i2 > i1:
            pieces.append(Piece("del", " ".join(a[i1:i2])))
        if j2 > j1:
            pieces.append(Piece("add", " ".join(b[j1:j2])))
    return pieces


@dataclass
class BulletView:
    source_id: str
    master: str
    texts: dict[int, str | None]  # candidate number -> text, None if the candidate omits it
    status: dict[int, Status]


@dataclass
class RoleView:
    role_id: str
    label: str
    bullets: list[BulletView]


@dataclass
class DiffView:
    company: str
    title: str
    versions: list[int]
    master_summary: str
    summaries: dict[int, str]
    master_strengths: list[str]
    strengths: dict[int, list[str]]
    roles: list[RoleView] = field(default_factory=list)

    def counts(self, n: int) -> tuple[int, int, int]:
        """(reworded, unchanged, absent) bullets for candidate n."""
        statuses = [b.status[n] for r in self.roles for b in r.bullets]
        return statuses.count("reworded"), statuses.count("unchanged"), statuses.count("absent")


def _norm(text: str) -> str:
    return " ".join(text.split())


def build_diff(records: dict[int, CandidateRecord]) -> DiffView:
    """`records` maps candidate number -> its saved record (one or two of them)."""
    nums = sorted(records)
    first = records[nums[0]]
    master_text: dict[str, str] = {}
    for n in nums:
        for k, v in records[n].master_text.items():
            master_text.setdefault(k, v)
    view = DiffView(
        company=first.company,
        title=first.title,
        versions=nums,
        master_summary=master_text.get("summary", ""),
        summaries={n: " ".join(c.text for c in records[n].output.summary) for n in nums},
        master_strengths=first.master_strengths,
        strengths={n: records[n].output.core_strengths for n in nums},
    )
    role_ids: list[str] = []
    for n in nums:
        role_ids += [r.role_id for r in records[n].output.roles if r.role_id not in role_ids]
    labels = {k: v for n in nums for k, v in records[n].role_labels.items()}
    for role_id in sorted(role_ids, key=lambda r: list(labels).index(r) if r in labels else 99):
        by_n = {n: {b.source_id: b.text for r in records[n].output.roles if r.role_id == role_id for b in r.bullets} for n in nums}  # fmt: skip
        order: list[str] = []
        for n in nums:
            order += [sid for sid in by_n[n] if sid not in order]
        bullets = []
        for sid in order:
            master = master_text.get(sid, "")
            texts = {n: by_n[n].get(sid) for n in nums}
            status: dict[int, Status] = {
                n: "absent" if t is None else "unchanged" if _norm(t) == _norm(master) else "reworded"
                for n, t in texts.items()
            }  # fmt: skip
            bullets.append(BulletView(sid, master, texts, status))
        view.roles.append(RoleView(role_id, labels.get(role_id, role_id), bullets))
    return view


# --- terminal ----------------------------------------------------------------------------


def diff_text(pieces: list[Piece]) -> Text:
    out = Text()
    for p in pieces:
        if out.plain:
            out.append(" ")
        style = {"same": "", "add": "bold green", "del": "red strike"}[p.op]
        out.append(p.text, style=style)
    return out


def render_terminal(view: DiffView, console) -> None:
    names = {n: f"candidate {n}" for n in view.versions}
    console.print(
        f"[bold]{view.title}[/bold] at {view.company}: master vs {' vs '.join(names.values())}"
    )
    for n in view.versions:
        reworded, unchanged, absent = view.counts(n)
        console.print(f"  {names[n]}: {reworded} bullets reworded, {unchanged} unchanged, {absent} not included")  # fmt: skip

    console.print("\n[bold]Summary[/bold]")
    for n in view.versions:
        console.print(
            Text(f"{names[n]}: ") + diff_text(word_diff(view.master_summary, view.summaries[n]))
        )

    console.print("\n[bold]Core strengths[/bold]")
    for n in view.versions:
        added = [s for s in view.strengths[n] if s not in view.master_strengths]
        kept = [s for s in view.strengths[n] if s in view.master_strengths]
        console.print(f"{names[n]}: {len(kept)} chosen from the master list" + (f", {len(added)} not in master (!)" if added else ""))  # fmt: skip

    for role in view.roles:
        changed = [b for b in role.bullets if any(s == "reworded" for s in b.status.values())]
        console.print(f"\n[bold]{role.label}[/bold]  ({len(role.bullets) - len(changed)} of {len(role.bullets)} bullets unchanged in every candidate)")  # fmt: skip
        for b in changed:
            console.print(f"  [dim]{b.source_id}[/dim]")
            for n in view.versions:
                text = b.texts[n]
                if text is None:
                    console.print(f"    {names[n]}: [dim](not included)[/dim]")
                elif b.status[n] == "unchanged":
                    console.print(f"    {names[n]}: [dim]unchanged[/dim]")
                else:
                    console.print(Text(f"    {names[n]}: ") + diff_text(word_diff(b.master, text)))
            if len(view.versions) == 2:
                a, c = (b.texts[n] for n in view.versions)
                if a and c and _norm(a) != _norm(c):
                    console.print(Text("    candidate 1 -> 2: ") + diff_text(word_diff(a, c)))


# --- HTML --------------------------------------------------------------------------------

_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px;color:#1a1a1a;max-width:1200px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 6px}.meta{color:#666;margin-bottom:14px}
table{border-collapse:collapse;width:100%;margin:6px 0}th,td{border:1px solid #ddd;padding:7px 9px;vertical-align:top;text-align:left}
th{background:#f4f4f4}td.id{color:#888;font-size:12px;width:90px}td.same{color:#777}td.absent{color:#bbb;font-style:italic}
ins{background:#d4f5d4;text-decoration:none;font-weight:600}del{background:#fbd5d5}
"""


def _html_diff(pieces: list[Piece]) -> str:
    tags = {"same": "{}", "add": "<ins>{}</ins>", "del": "<del>{}</del>"}
    return " ".join(tags[p.op].format(escape(p.text)) for p in pieces)


def render_html(view: DiffView) -> str:
    heads = "".join(f"<th>Candidate {n}</th>" for n in view.versions)
    parts = [f"<!doctype html><meta charset=utf-8><title>Diff: {escape(view.title)}</title><style>{_CSS}</style>",
             f"<h1>{escape(view.title)} at {escape(view.company)}</h1>"]  # fmt: skip
    stats = "; ".join(
        f"candidate {n}: {r} reworded, {u} unchanged"
        for n in view.versions
        for r, u, _ in [view.counts(n)]
    )
    parts.append(f"<div class=meta>Master vs {len(view.versions)} candidate(s). Green = added, red = removed vs the master. {escape(stats)}</div>")  # fmt: skip

    parts.append(
        f"<h2>Summary</h2><table><tr><th>Master</th>{heads}</tr><tr><td>{escape(view.master_summary)}</td>"
    )
    parts += [
        f"<td>{_html_diff(word_diff(view.master_summary, view.summaries[n]))}</td>"
        for n in view.versions
    ]
    parts.append("</tr></table>")

    parts.append(f"<h2>Core strengths</h2><table><tr>{heads}</tr><tr>")
    parts += [f"<td>{escape(' · '.join(view.strengths[n]))}</td>" for n in view.versions]
    parts.append("</tr></table>")

    for role in view.roles:
        parts.append(
            f"<h2>{escape(role.label)}</h2><table><tr><th>id</th><th>Master</th>{heads}</tr>"
        )
        for b in role.bullets:
            cells = []
            for n in view.versions:
                text, status = b.texts[n], b.status[n]
                if text is None:
                    cells.append("<td class=absent>not included</td>")
                elif status == "unchanged":
                    cells.append("<td class=same>unchanged</td>")
                else:
                    cells.append(f"<td>{_html_diff(word_diff(b.master, text))}</td>")
            parts.append(
                f"<tr><td class=id>{escape(b.source_id)}</td><td>{escape(b.master)}</td>{''.join(cells)}</tr>"
            )
        parts.append("</table>")
    return "\n".join(parts)
