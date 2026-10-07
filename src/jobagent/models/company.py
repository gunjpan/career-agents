from typing import Literal

from pydantic import BaseModel

COMPANIES_HEADERS = ["name", "tier", "levels", "ats", "board_id", "status", "notes"]

CompanyStatus = Literal["pending", "active", "needs_review", "paused"]


class Company(BaseModel):
    """One row of the Companies tab. Adding a company = adding a row."""

    name: str
    tier: str = ""
    levels: list[str] = []
    ats: str = ""
    board_id: str = ""
    status: CompanyStatus = "pending"
    notes: str = ""

    @classmethod
    def from_record(cls, rec: dict[str, str]) -> "Company":
        return cls(
            name=rec["name"].strip(),
            tier=rec.get("tier", "").strip(),
            levels=[x.strip() for x in rec.get("levels", "").split(",") if x.strip()],
            ats=rec.get("ats", "").strip().lower(),
            board_id=rec.get("board_id", "").strip(),
            status=rec.get("status", "").strip().lower() or "pending",
            notes=rec.get("notes", "").strip(),
        )

    def to_record(self) -> dict[str, str]:
        return {
            "name": self.name,
            "tier": self.tier,
            "levels": ",".join(self.levels),
            "ats": self.ats,
            "board_id": self.board_id,
            "status": self.status,
            "notes": self.notes,
        }
