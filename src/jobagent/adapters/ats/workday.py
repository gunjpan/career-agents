import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from jobagent.adapters.ats.base import html_to_text
from jobagent.models.company import Company
from jobagent.models.criteria import DiscoveryCriteria
from jobagent.models.job import RawPosting

# Workday's job-board JSON API is unofficial (the one its own career sites call), so it can
# change without notice. Everything Workday-specific lives in this file.
#
# Company row: ats=workday, board_id="<wdN>/<tenant>/<site>", e.g. "wd3/rbc/RBCGLOBAL1"
# (host https://<tenant>.<wdN>.myworkdayjobs.com, site is the path segment after the host).

PAGE_SIZE = 20
MAX_PAGES_PER_TERM = 15
COUNTRY_FACETS = ("Country", "locationCountry")
FAMILY_FACETS = ("Category", "jobFamilyGroup")  # top-level job family, named differently per tenant
_RELATIVE_DATE = re.compile(r"posted\s+(today|yesterday|(\d+)(\+)?\s+days?\s+ago)", re.IGNORECASE)
_MULTI_LOCATION = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)
_POSTING_URL = re.compile(
    r"^https://(?P<tenant>[^.]+)\.(?P<wd>[^.]+)\.myworkdayjobs\.com/(?P<site>[^/]+)(?P<path>/job/.+)$"
)


def parse_board_id(board_id: str) -> tuple[str, str, str]:
    parts = board_id.split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"workday board_id must be '<wdN>/<tenant>/<site>', got {board_id!r}")
    return parts[0], parts[1], parts[2]


def relative_posted_at(text: str, now: datetime) -> datetime | None:
    """'Posted Today' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago' -> approximate datetime.

    '30+' is treated as 31 days: always at or below the true age, so a larger max_age_days
    never wrongly rejects it, and the exact date comes from enrich() anyway.
    """
    m = _RELATIVE_DATE.search(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if word == "today":
        days = 0
    elif word == "yesterday":
        days = 1
    else:
        days = int(m.group(2)) + (1 if m.group(3) else 0)
    return now - timedelta(days=days)


def find_country_facet(facets: list[dict], country: str) -> tuple[str, str] | None:
    """Locate (facetParameter, value id) for a country, searching nested facet groups."""
    for facet in facets:
        values = facet.get("values", [])
        if facet.get("facetParameter") in COUNTRY_FACETS:
            for v in values:
                if v.get("descriptor", "").lower() == country.lower() and "id" in v:
                    return facet["facetParameter"], v["id"]
        nested = [v for v in values if "facetParameter" in v]
        if nested and (found := find_country_facet(nested, country)):
            return found
    return None


def find_family_values(
    facets: list[dict], patterns: list[str]
) -> tuple[str, list[tuple[str, str]]] | None:
    """The tenant's job-family facet and the (name, id) of values matching any pattern.

    None means the tenant has no recognised family facet (caller falls back to unfiltered).
    """
    for facet in facets:
        values = facet.get("values", [])
        if facet.get("facetParameter") in FAMILY_FACETS:
            matches = [
                (v["descriptor"], v["id"])
                for v in values
                if "id" in v and any(re.search(p, v["descriptor"], re.IGNORECASE) for p in patterns)
            ]
            return facet["facetParameter"], matches
        nested = [v for v in values if "facetParameter" in v]
        if nested and (found := find_family_values(nested, patterns)):
            return found
    return None


class WorkdayAdapter:
    name = "workday"

    def __init__(
        self,
        client: httpx.Client,
        discovery: DiscoveryCriteria,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        request_delay: float = 0.3,
    ) -> None:
        self.client = client
        self.discovery = discovery
        self.clock = clock
        self.sleep = sleep
        self.request_delay = request_delay

    def _api(self, wd: str, tenant: str, site: str) -> str:
        return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"

    def _post_jobs(self, api: str, facets: dict, offset: int, limit: int, term: str) -> dict:
        self.sleep(self.request_delay)
        body = {"appliedFacets": facets, "limit": limit, "offset": offset, "searchText": term}
        resp = self.client.post(f"{api}/jobs", json=body)
        resp.raise_for_status()
        return resp.json()

    def probe(self, company: Company) -> int:
        """How many postings the board reports, from one request (used to validate a new board)."""
        wd, tenant, site = parse_board_id(company.board_id)
        data = self._post_jobs(self._api(wd, tenant, site), {}, 0, 1, "")
        return int(data.get("total", 0))

    def fetch(self, company: Company) -> list[RawPosting]:
        wd, tenant, site = parse_board_id(company.board_id)
        api = self._api(wd, tenant, site)
        patterns = self.discovery.job_family_patterns

        probe_facets: list[dict] = []
        if self.discovery.country or patterns:
            probe_facets = self._post_jobs(api, {}, 0, 1, "").get("facets", [])

        base: dict = {}
        if self.discovery.country:
            found = find_country_facet(probe_facets, self.discovery.country)
            if found:
                base = {found[0]: [found[1]]}
            # No country facet on this tenant: fall back to unfiltered; the location filter
            # in the pipeline still removes foreign postings.

        # One search per matching job family so each posting can be tagged with its family.
        searches: list[tuple[str, dict]] = [("", base)]
        if patterns and (fam := find_family_values(probe_facets, patterns)):
            param, matches = fam
            searches = [(name, {**base, param: [fid]}) for name, fid in matches]
        # No family facet on this tenant: unfiltered, department stays empty.

        public = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
        found_postings: dict[str, RawPosting] = {}
        now = self.clock()
        for department, facets in searches:
            for term in self.discovery.search_terms or [""]:
                self._search(api, public, company, facets, term, department, now, found_postings)
        return list(found_postings.values())

    def _search(
        self,
        api: str,
        public: str,
        company: Company,
        facets: dict,
        term: str,
        department: str,
        now: datetime,
        found: dict[str, RawPosting],
    ) -> None:
        total = None
        for page in range(MAX_PAGES_PER_TERM):
            offset = page * PAGE_SIZE
            data = self._post_jobs(api, facets, offset, PAGE_SIZE, term)
            if total is None:
                total = data.get("total", 0)  # Workday reports 0 on every later page
            for raw in data.get("jobPostings", []):
                path = raw.get("externalPath")
                if not path or "title" not in raw:
                    continue  # stub entries (only bulletFields) have nothing to link to
                if path not in found:
                    found[path] = self._parse_list(company, public, raw, now, department)
            if not data.get("jobPostings") or offset + PAGE_SIZE >= total:
                break

    def _parse_list(
        self, company: Company, public: str, raw: dict, now: datetime, department: str = ""
    ) -> RawPosting:
        loc = (raw.get("locationsText") or "").strip()
        bullets = raw.get("bulletFields") or []
        return RawPosting(
            company=company.name,
            ats=self.name,
            external_id=bullets[0] if bullets else raw["externalPath"],
            title=raw["title"],
            # "2 Locations" is not a location; enrich() fills the real ones in.
            locations=[] if not loc or _MULTI_LOCATION.match(loc) else [loc],
            url=public + raw["externalPath"],
            posted_at=relative_posted_at(raw.get("postedOn", ""), now),
            department=department,
        )

    def enrich(self, posting: RawPosting) -> RawPosting:
        """One request per posting: exact start date, all locations and the full description."""
        m = _POSTING_URL.match(posting.url)
        if not m:
            raise ValueError(f"not a workday posting url: {posting.url}")
        api = self._api(m["wd"], m["tenant"], m["site"])
        self.sleep(self.request_delay)
        resp = self.client.get(api + m["path"])
        resp.raise_for_status()
        info = resp.json()["jobPostingInfo"]

        locations = [info["location"]] if info.get("location") else []
        locations += [x for x in info.get("additionalLocations") or [] if x]
        start = info.get("startDate")
        remote = (info.get("remoteType") or "").lower() or None
        return posting.model_copy(
            update={
                "locations": locations or posting.locations,
                "posted_at": datetime.fromisoformat(start).replace(tzinfo=UTC)
                if start
                else posting.posted_at,
                "workplace_type": remote,
                "remote": True if remote == "remote" else posting.remote,
                "description": html_to_text(info.get("jobDescription") or ""),
            }
        )
