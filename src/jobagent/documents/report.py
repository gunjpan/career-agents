from jobagent.models.tailoring import TailorOutput, VerifierOutput


def render_report(
    *,
    status: str,
    company: str,
    title: str,
    level: str,
    history: list[str],
    verifier: VerifierOutput | None,
    warnings: list[str],
    draft: TailorOutput | None,
    cost_usd: float,
    versions: dict[str, str],
) -> str:
    """The Verifier's report, saved next to the documents so you can audit every claim."""
    lines = [f"# Verifier report: {title} at {company}", ""]
    lines += [f"- **Status:** {status.upper()}", f"- **Role level (Scorer):** {level}"]
    lines += [f"- **Cost:** ${cost_usd:.4f}"] + [f"- **{k}:** {v}" for k, v in versions.items()]
    lines += ["", "## Attempts"] + [f"- {h}" for h in history]
    if verifier is not None:
        lines += ["", "## Claim checks", "", "| Verdict | Claim | Note |", "|---|---|---|"]
        for c in verifier.checks:
            claim, note = c.claim.replace("|", "/")[:110], c.note.replace("|", "/")
            lines.append(f"| {c.verdict} | {claim} | {note} |")
        lines += [
            "",
            f"## Level framing: {'OK' if verifier.level_framing_ok else 'PROBLEM'}",
            verifier.level_framing_note,
        ]
    if warnings:
        lines += ["", "## Warnings (did not block)"] + [f"- {w}" for w in warnings]
    if status != "ready" and draft is not None:
        lines += ["", "## Last draft (not approved for use)"]
        lines += [f"- {c.text}" for c in draft.summary]
    return "\n".join(lines) + "\n"
