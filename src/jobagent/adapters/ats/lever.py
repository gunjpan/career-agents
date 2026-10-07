from datetime import UTC, datetime

import httpx

from jobagent.models.company import Company
from jobagent.models.job import RawPosting

API = "https://api.lever.co/v0/postings/{board_id}?mode=json"


class LeverAdapter:
    name = "lever"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, company: Company) -> list[RawPosting]:
        resp = self.client.get(API.format(board_id=company.board_id))
        resp.raise_for_status()
        return [self._parse(company, j) for j in resp.json()]

    def _parse(self, company: Company, j: dict) -> RawPosting:
        cats = j.get("categories") or {}
        locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        created = j.get("createdAt")  # epoch milliseconds
        workplace = (j.get("workplaceType") or "").lower() or None
        return RawPosting(
            company=company.name,
            ats=self.name,
            external_id=j["id"],
            title=j["text"],
            locations=locations,
            url=j["hostedUrl"],
            posted_at=datetime.fromtimestamp(created / 1000, tz=UTC) if created else None,
            remote=True if workplace == "remote" else None,
            workplace_type=workplace,
            description=j.get("descriptionPlain") or "",
        )
