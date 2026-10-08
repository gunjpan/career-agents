from dataclasses import dataclass, field


@dataclass
class ExperienceEntry:
    company: str  # empty for the "current focus" block
    title: str
    dates: str
    bullets: list[str]


@dataclass
class ResumeDoc:
    name: str
    contact: str  # "Toronto, ON · 555-0100 · me@example.com"
    headline: str
    summary: str
    core_strengths: list[str]
    experience: list[ExperienceEntry]
    education: list[str] = field(default_factory=list)


@dataclass
class CoverLetterDoc:
    name: str
    contact: str
    date: str
    regarding: str  # "Director, Engineering - Acme"
    greeting: str
    paragraphs: list[str]
    closing: str = "Sincerely,"
