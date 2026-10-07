from datetime import datetime

import httpx

from jobagent.adapters.ats.base import html_to_text
from jobagent.models.company import Company
from jobagent.models.job import RawPosting

API = "https://boards-api.greenhouse.io/v1/boards/{board_id}/jobs?content=true"


class GreenhouseAdapter:
    name = "greenhouse"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, company: Company) -> list[RawPosting]:
        resp = self.client.get(API.format(board_id=company.board_id))
        resp.raise_for_status()
        return [self._parse(company, j) for j in resp.json()["jobs"]]

    def _parse(self, company: Company, j: dict) -> RawPosting:
        loc = (j.get("location") or {}).get("name")
        # `first_published` is the real posted date; `updated_at` changes on every edit, so unused.
        published = j.get("first_published")
        return RawPosting(
            company=company.name,
            ats=self.name,
            external_id=str(j["id"]),
            title=j["title"],
            locations=[loc] if loc else [],
            url=j["absolute_url"],
            posted_at=datetime.fromisoformat(published) if published else None,
            description=html_to_text(j.get("content") or ""),
        )
