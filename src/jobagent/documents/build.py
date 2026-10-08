import re
from datetime import date

from jobagent.agents.context import CURRENT_FOCUS_ID
from jobagent.documents.model import CoverLetterDoc, ExperienceEntry, ResumeDoc
from jobagent.models.tailoring import TailorOutput

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def plain_dashes(text: str) -> str:
    """Em and en dashes -> plain hyphens, in the documents only (the master stays as written).

    'Engineering — Wealth' -> 'Engineering - Wealth'; '20–25%' -> '20-25%'; 'Nov – Sep' -> 'Nov - Sep'.
    """
    text = re.sub(r"\s*—\s*", " - ", text)  # em dash, spaced or not
    text = re.sub(r"\s+–\s+", " - ", text)  # spaced en dash
    return text.replace("–", "-")  # en dash inside a range: 20–25


def month_year(ym: str) -> str:
    """'2021-11' -> 'Nov 2021'."""
    year, month = str(ym).split("-")
    return f"{_MONTHS[int(month) - 1]} {year}"


def contact_line(profile: dict) -> str:
    return " · ".join(
        filter(
            None,
            [
                profile.get("location"),
                profile.get("phone"),
                profile.get("email"),
                profile.get("linkedin"),
            ],
        )
    )


def _roles_by_id(resume: dict) -> dict[str, tuple[dict, dict]]:
    return {r["id"]: (c, r) for c in resume["experience"] for r in c["roles"]}


def build_resume(resume: dict, out: TailorOutput) -> ResumeDoc:
    roles = _roles_by_id(resume)
    entries: list[tuple[str, ExperienceEntry]] = []
    for tailored in out.roles:
        bullets = [plain_dashes(b.text.strip()) for b in tailored.bullets]
        if tailored.role_id == CURRENT_FOCUS_ID:
            focus = resume["current_focus"]
            entries.append(
                (
                    "9999-99",
                    ExperienceEntry(
                        "", plain_dashes(f"Current focus: {focus['title']}"), "", bullets
                    ),
                )
            )
            continue
        company, role = roles[tailored.role_id]
        dates = f"{month_year(role['start'])} - {month_year(role['end'])}"
        entries.append(
            (
                str(role["start"]),
                ExperienceEntry(
                    plain_dashes(company["company"]), plain_dashes(role["title"]), dates, bullets
                ),
            )
        )
    entries.sort(key=lambda e: e[0], reverse=True)  # newest first; current focus on top

    profile = resume["profile"]
    return ResumeDoc(
        name=profile["name"],
        contact=contact_line(profile),
        headline=plain_dashes(profile.get("headline", "")),
        summary=plain_dashes(" ".join(c.text.strip() for c in out.summary)),
        core_strengths=[plain_dashes(s.strip()) for s in out.core_strengths],
        experience=[e for _, e in entries],
        education=[
            plain_dashes(f"{e['degree']}, {e['school']}") for e in resume.get("education", [])
        ],
    )


def build_cover_letter(
    resume: dict, out: TailorOutput, *, company: str, title: str, today: date
) -> CoverLetterDoc:
    profile = resume["profile"]
    return CoverLetterDoc(
        name=profile["name"],
        contact=contact_line(profile),
        date=today.strftime("%B %d, %Y").replace(" 0", " "),
        regarding=plain_dashes(f"{title} - {company}"),
        greeting="Dear Hiring Team,",
        paragraphs=[plain_dashes(c.text.strip()) for c in out.cover_letter],
    )
