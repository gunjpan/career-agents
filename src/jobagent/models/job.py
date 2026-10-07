from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobState(StrEnum):
    NEW = "new"
    FILTERED = "filtered"
    SCORED = "scored"
    SHORTLISTED = "shortlisted"
    APPROVED = "approved"
    TAILORED = "tailored"
    VERIFIED = "verified"
    READY = "ready"
    SUBMITTED = "submitted"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    CLOSED = "closed"


class RawPosting(BaseModel):
    """What every ATS adapter returns, whatever the source API looks like."""

    company: str
    ats: str
    external_id: str
    title: str
    locations: list[str] = Field(default_factory=list)  # primary first; may be empty
    url: str
    posted_at: datetime | None = None  # None when the ATS gives no reliable posted date
    remote: bool | None = None  # None = ATS doesn't say
    workplace_type: str | None = None  # remote / hybrid / onsite, lowercased, if known
    description: str = ""  # plain text


class Job(BaseModel):
    """A posting that survived the hard filters, tracked through the pipeline."""

    job_id: str
    dedupe_key: str
    state: JobState = JobState.NEW
    flags: list[str] = Field(default_factory=list)  # e.g. date_unknown, location_unknown
    first_seen: datetime
    posting: RawPosting

    def to_record(self) -> dict[str, str]:
        p = self.posting
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "company": p.company,
            "title": p.title,
            "location": " | ".join(p.locations),
            "workplace_type": p.workplace_type or "",
            "posted_at": p.posted_at.isoformat() if p.posted_at else "",
            "flags": ",".join(self.flags),
            "url": p.url,
            "ats": p.ats,
            "external_id": p.external_id,
            "dedupe_key": self.dedupe_key,
            "first_seen": self.first_seen.isoformat(timespec="seconds"),
            "description": p.description[:DESCRIPTION_CELL_LIMIT],
        }


# Sheets cells cap at 50,000 characters; leave headroom.
DESCRIPTION_CELL_LIMIT = 30_000

JOBS_HEADERS = [
    "job_id",
    "state",
    "company",
    "title",
    "location",
    "workplace_type",
    "posted_at",
    "flags",
    "url",
    "ats",
    "external_id",
    "dedupe_key",
    "first_seen",
    "description",
]
