"""Turns config files into the text the Scorer sees. Pure functions, easy to test."""

from pathlib import Path

import yaml


def load_yaml(path: str | Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _tags(node: dict) -> list[str]:
    out = []
    if node.get("tech_stack"):
        out.append("- tech stack: " + ", ".join(node["tech_stack"]))
    if node.get("skills"):
        out.append("- skills: " + ", ".join(node["skills"]))
    return out


def render_resume(resume: dict) -> str:
    """Master resume -> compact text. Contact details (the `profile` block) are never included:
    the model does not need them to judge fit."""
    lines = ["# Candidate profile", "", "Summary:", " ".join(resume["summary"].split()), ""]
    lines.append("Core strengths: " + "; ".join(resume["core_strengths"]))
    for company in resume["experience"]:
        lines += ["", f"## {company['company']} ({company['start']} to {company['end']})"]
        if company.get("summary"):
            lines.append(" ".join(company["summary"].split()))
        lines += _tags(company)
        for role in company["roles"]:
            lines.append(f"### {role['title']} ({role['start']} to {role['end']})")
            lines += _tags(role)
            for key, value in (role.get("scope") or {}).items():
                lines.append(f"- scope.{key}: {' '.join(str(value).split())}")
            lines += [f"- {' '.join(b['text'].split())}" for b in role["bullets"]]
    focus = resume.get("current_focus")
    if focus:
        lines += ["", f"## Current focus: {focus['title']}"]
        lines += [f"- {' '.join(b['text'].split())}" for b in focus["bullets"]]
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
