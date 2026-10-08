from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from jobagent.models.scoring import ScoreResult


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
    department: str = ""  # department / job family as the ATS names it; a Scorer feature later
    description: str = ""  # plain text


class Job(BaseModel):
    """A posting that survived the hard filters, tracked through the pipeline."""

    job_id: str
    dedupe_key: str
    state: JobState = JobState.NEW
    flags: list[str] = Field(default_factory=list)  # e.g. date_unknown, location_unknown
    first_seen: datetime
    posting: RawPosting
    score: ScoreResult | None = None

    @classmethod
    def from_record(cls, rec: dict[str, str]) -> "Job":
        """Rebuild a Job from a Jobs-tab row (score columns are not read back)."""
        posted = rec.get("posted_at") or ""
        posting = RawPosting(
            company=rec["company"],
            ats=rec.get("ats", ""),
            external_id=rec.get("external_id", ""),
            title=rec["title"],
            locations=[x for x in rec.get("location", "").split(" | ") if x],
            url=rec.get("url", ""),
            posted_at=datetime.fromisoformat(posted) if posted else None,
            workplace_type=rec.get("workplace_type") or None,
            department=rec.get("department", ""),
            description=rec.get("description", ""),
        )
        return cls(
            job_id=rec["job_id"],
            dedupe_key=rec.get("dedupe_key", ""),
            state=JobState(rec["state"]),
            flags=[x for x in rec.get("flags", "").split(",") if x],
            first_seen=datetime.fromisoformat(rec["first_seen"]),
            posting=posting,
        )

    def score_record(self) -> dict[str, str]:
        """Only the columns scoring changes: state plus the score columns."""
        out = {"state": self.state.value}
        if self.score is None:
            return out
        r, o = self.score, self.score.output
        return out | {
            "real_level": o.real_level,
            "level_confidence": o.level_confidence,
            "level_evidence": " | ".join(o.level_evidence),
            "title_matches_level": str(o.title_matches_level).lower(),
            "function": o.function,
            "core_domain_covered": str(o.core_domain_covered).lower(),
            "fit_score": str(o.fit_score),
            "rationale": o.rationale,
            "strengths": " | ".join(o.strengths),
            "gaps": " | ".join(o.gaps),
            "scorer_prompt_version": str(r.prompt_version),
            "scorer_model": r.model,
            "score_cost_usd": f"{r.cost_usd:.6f}",
            "scored_at": r.scored_at.isoformat(timespec="seconds"),
        }

    def to_record(self) -> dict[str, str]:
        p = self.posting
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "company": p.company,
            "title": p.title,
            "location": " | ".join(p.locations),
            "department": p.department,
            "workplace_type": p.workplace_type or "",
            "posted_at": p.posted_at.isoformat() if p.posted_at else "",
            "flags": ",".join(self.flags),
            "url": p.url,
            "ats": p.ats,
            "external_id": p.external_id,
            "dedupe_key": self.dedupe_key,
            "first_seen": self.first_seen.isoformat(timespec="seconds"),
            "description": p.description[:DESCRIPTION_CELL_LIMIT],
        } | self.score_record()


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
    "department",  # added after the first release; new columns go at the end (additive migration)
    # Scorer output (Block 3)
    "real_level",
    "level_confidence",
    "level_evidence",
    "title_matches_level",
    "function",
    "fit_score",
    "rationale",
    "strengths",
    "gaps",
    "scorer_prompt_version",
    "scorer_model",
    "score_cost_usd",
    "scored_at",
    "core_domain_covered",
    # Tailor and Verifier (Block 4)
    "tailor_status",  # ready | blocked | error
    "drive_url",
    "tailor_attempts",
    "tailor_cost_usd",
    "tailor_notes",
    "tailor_candidates",  # JSON manifest of the live candidate versions
    "final_candidate",  # the candidate number to apply with; blank while two are awaiting a pick
    "tailor_total_cost_usd",  # running total over every run, including blocked ones and trashed candidates
]
