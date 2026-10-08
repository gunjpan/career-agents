from datetime import datetime

from pydantic import BaseModel, Field

from jobagent.models.tailoring import TailorOutput

MAX_LIVE_CANDIDATES = 2  # compare two versions, pick one; never an ever-growing pile


class CandidateMeta(BaseModel):
    """One saved version of a job's resume. Lives in the manifest on the Jobs tab."""

    n: int  # 1, 2, 3...: never reused, so candidate-3 can follow a picked candidate-1
    folder_id: str
    folder_url: str
    json_file_id: str  # the CandidateRecord stored next to the documents
    created_at: datetime
    tailor_prompt_version: int
    verifier_prompt_version: int
    keep: str  # the `keep` setting used (all_bullets, all_roles, tailor_choice)
    cost_usd: float


class Manifest(BaseModel):
    """All live candidates for one job, stored as JSON in the `tailor_candidates` column."""

    job_folder_id: str = ""
    job_folder_url: str = ""
    candidates: list[CandidateMeta] = Field(default_factory=list)

    @classmethod
    def from_cell(cls, cell: str | None) -> "Manifest":
        return cls.model_validate_json(cell) if cell else cls()

    def to_cell(self) -> str:
        return self.model_dump_json()

    @property
    def next_n(self) -> int:
        return max((c.n for c in self.candidates), default=0) + 1

    @property
    def full(self) -> bool:
        return len(self.candidates) >= MAX_LIVE_CANDIDATES

    def get(self, n: int) -> CandidateMeta | None:
        return next((c for c in self.candidates if c.n == n), None)


class CandidateRecord(BaseModel):
    """`tailored.json`: what was produced and the master text it was built from."""

    job_id: str
    company: str
    title: str
    output: TailorOutput  # the completed output, i.e. what the documents contain
    master_text: dict[str, str]  # bullet id (and "summary") -> master text at that time
    master_strengths: list[str]
    role_labels: dict[str, str]  # role id -> "Title (Company)" for display
