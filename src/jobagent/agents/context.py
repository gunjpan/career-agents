"""Turns config files into the text the Scorer sees. Pure functions, easy to test."""

from pathlib import Path

import yaml


def load_yaml(path: str | Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


CURRENT_FOCUS_ID = "current-focus"  # the master resume's current_focus block, treated as a role
SUMMARY_ID = "summary"  # the master summary, citable by summary and cover-letter claims


def _tags(node: dict) -> list[str]:
    out = []
    if node.get("tech_stack"):
        out.append("- tech stack: " + ", ".join(node["tech_stack"]))
    if node.get("skills"):
        out.append("- skills: " + ", ".join(node["skills"]))
    return out


def _bullet(b: dict, include_ids: bool) -> str:
    text = " ".join(b["text"].split())
    return f"- [{b['id']}] {text}" if include_ids else f"- {text}"


def render_resume(resume: dict, *, include_ids: bool = False) -> str:
    """Master resume -> compact text. Contact details (the `profile` block) are never included:
    the model does not need them to judge fit."""
    tag = " [summary]" if include_ids else ""
    lines = ["# Candidate profile", "", f"Summary{tag}:", " ".join(resume["summary"].split()), ""]
    lines.append("Core strengths: " + "; ".join(resume["core_strengths"]))
    for company in resume["experience"]:
        lines += ["", f"## {company['company']} ({company['start']} to {company['end']})"]
        if company.get("summary"):
            lines.append(" ".join(company["summary"].split()))
        lines += _tags(company)
        for role in company["roles"]:
            tag = f" [role {role['id']}]" if include_ids else ""
            lines.append(f"### {role['title']} ({role['start']} to {role['end']}){tag}")
            lines += _tags(role)
            for key, value in (role.get("scope") or {}).items():
                lines.append(f"- scope.{key}: {' '.join(str(value).split())}")
            lines += [_bullet(b, include_ids) for b in role["bullets"]]
    focus = resume.get("current_focus")
    if focus:
        tag = f" [role {CURRENT_FOCUS_ID}]" if include_ids else ""
        lines += ["", f"## Current focus: {focus['title']}{tag}"]
        lines += [_bullet(b, include_ids) for b in focus["bullets"]]
    for edu in resume.get("education", []):
        lines.append(f"\nEducation: {edu['degree']}, {edu['school']}")
    extra = resume.get("additional_facts") or []
    if extra:
        lines += ["", "Additional facts:"] + [f"- {x}" for x in extra]
    return "\n".join(lines)


def render_profiles(profiles: dict) -> str:
    lines = ["# Level definitions"]
    for name, p in profiles["levels"].items():
        lines += ["", f"## {name}", "Typical titles: " + ", ".join(p["title_variants"])]
        lines += [f"- {k}: {v}" for k, v in p["scope_signals"].items()]
    lines += ["", "# Title-inflation notes"] + [f"- {n}" for n in profiles["title_inflation_notes"]]
    return "\n".join(lines)


def render_posting(job, max_chars: int) -> str:
    """The posting as the model sees it, wrapped in tags so it reads as data, not instructions."""
    p = job.posting
    text = p.description
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[posting truncated]"
    header = [
        f"Company: {p.company}",
        f"Title: {p.title}",
        f"Department: {p.department or 'unknown'}",
        f"Location: {' | '.join(p.locations) or 'unknown'}",
        f"Workplace type: {p.workplace_type or 'unknown'}",
    ]
    return "<posting>\n" + "\n".join(header) + f"\n\n{text}\n</posting>"
