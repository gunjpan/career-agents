from datetime import datetime

import httpx

from jobagent.models.company import Company
from jobagent.models.job import RawPosting

API = "https://api.ashbyhq.com/posting-api/job-board/{board_id}"


class AshbyAdapter:
    name = "ashby"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, company: Company) -> list[RawPosting]:
        resp = self.client.get(API.format(board_id=company.board_id))
        resp.raise_for_status()
        return [self._parse(company, j) for j in resp.json()["jobs"] if j.get("isListed", True)]

    def _parse(self, company: Company, j: dict) -> RawPosting:
        locations = [j["location"]] if j.get("location") else []
        for sec in j.get("secondaryLocations") or []:
            name = sec.get("location") if isinstance(sec, dict) else sec
            if name:
                locations.append(name)
        published = j.get("publishedAt")
        return RawPosting(
            company=company.name,
            ats=self.name,
            external_id=j["id"],
            title=j["title"],
            locations=locations,
            url=j["jobUrl"],
            posted_at=datetime.fromisoformat(published) if published else None,
            remote=j.get("isRemote"),
            workplace_type=(j.get("workplaceType") or "").lower() or None,
            department=j.get("department") or "",
            description=j.get("descriptionPlain") or "",
        )
