"""Find a company's job platform (ATS) and board id from its careers pages. Plain code, no model."""

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

SUPPORTED = {"greenhouse", "lever", "ashby", "workday"}  # we have adapters for these
_SLUG = r"([A-Za-z0-9][A-Za-z0-9_.%-]{0,60})"
_RESERVED = {"embed", "api", "v1", "login", "wday", "cxs", "en-us", "assets", "static", "undefined"}

PATTERNS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(
        rf"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?{_SLUG}"
        rf"|boards-api\.greenhouse\.io/v1/boards/{_SLUG}",
        re.IGNORECASE,
    ),
    "lever": re.compile(
        rf"jobs(?:\.eu)?\.lever\.co/{_SLUG}|api\.lever\.co/v0/postings/{_SLUG}", re.IGNORECASE
    ),
    "ashby": re.compile(
        rf"jobs\.ashbyhq\.com/{_SLUG}|api\.ashbyhq\.com/posting-api/job-board/{_SLUG}",
        re.IGNORECASE,
    ),
    # board id for Workday is "<wdN>/<tenant>/<site>", see adapters/ats/workday.py
    "workday": re.compile(
        r"https?://([A-Za-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[A-Za-z0-9-]+/)?"
        r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
    # Recognised but not supported yet: reported, so the user knows why a company needs review.
    "smartrecruiters": re.compile(
        rf"(?:jobs|careers)\.smartrecruiters\.com/{_SLUG}", re.IGNORECASE
    ),
    "icims": re.compile(r"([A-Za-z0-9-]+)\.icims\.com", re.IGNORECASE),
    "taleo": re.compile(r"([A-Za-z0-9-]+)\.taleo\.net", re.IGNORECASE),
    "oracle": re.compile(r"([A-Za-z0-9-]+)\.fa\.[a-z0-9-]+\.oraclecloud\.com", re.IGNORECASE),
    "successfactors": re.compile(r"([A-Za-z0-9-]+)\.successfactors\.(?:com|eu)", re.IGNORECASE),
    "phenom": re.compile(r"(cdn\.phenompeople\.com)", re.IGNORECASE),
}


@dataclass(frozen=True)
class Signature:
    ats: str
    board_id: str
    count: int  # how many times the page links to it

    @property
    def supported(self) -> bool:
        return self.ats in SUPPORTED


def _board_id(ats: str, groups: tuple[str | None, ...]) -> str | None:
    found = [g for g in groups if g]
    if not found:
        return None
    if ats == "workday":
        tenant, wd, site = found[0].lower(), found[1].lower(), found[2]
        return None if site.lower() in _RESERVED else f"{wd}/{tenant}/{site}"
    slug = found[0].rstrip(".")
    return None if slug.lower() in _RESERVED else slug


def find_signatures(html: str) -> list[Signature]:
    """Every job-platform link on a page, most frequent first, supported platforms before others."""
    counts: Counter[tuple[str, str]] = Counter()
    for ats, pattern in PATTERNS.items():
        for m in pattern.finditer(html):
            board_id = _board_id(ats, m.groups())
            if board_id:
                counts[(ats, board_id)] += 1
    sigs = [Signature(ats, board, n) for (ats, board), n in counts.items()]
    return sorted(sigs, key=lambda s: (not s.supported, -s.count, s.ats))


_EMBEDDED = {
    "greenhouse": re.compile(r"[?&]gh_jid=(\d+)"),  # Greenhouse embeds on the company's own site
    "ashby": re.compile(r"[?&]ashby_jid=([0-9a-fA-F-]{36})"),
}


def embedded_job_ids(html: str) -> dict[str, set[str]]:
    """Job ids that a company's own pages pass to an embedded job board, by platform."""
    return {ats: ids for ats, rx in _EMBEDDED.items() if (ids := set(rx.findall(html)))}


def host_matches(url: str, domain: str) -> bool:
    """Is `url` on `domain` or one of its subdomains?"""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    domain = domain.lower().removeprefix("www.").strip("/")
    return bool(domain) and (host == domain or host.endswith("." + domain))


def fetch_page(client: httpx.Client, url: str) -> tuple[str, str] | None:
    """(final url, html) for a normal page, or None. Network trouble is a 'no', not a crash."""
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
        return None
    return str(resp.url), resp.text[:2_000_000]


def slug_candidates(name: str, *hints: str) -> list[str]:
    """Plausible board slugs for a company: hints first, then variants of its name."""
    base = re.sub(
        r"\b(inc|ltd|llc|corp|corporation|co|company|limited|plc|the)\b\.?", " ", name.lower()
    )
    words = re.findall(r"[a-z0-9]+", base)
    out: list[str] = []
    for cand in [*hints, "".join(words), "-".join(words), words[0] if words else ""]:
        cand = (
            cand.strip().lower().removeprefix("www.").split(".")[0]
            if "." in cand and " " not in cand
            else cand.strip().lower()
        )
        if cand and cand not in out:
            out.append(cand)
    return out


PROBES = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


@dataclass(frozen=True)
class Hit:
    ats: str
    slug: str
    job_ids: frozenset[str]
    board_name: str = ""  # only Greenhouse publishes one


def name_key(text: str) -> str:
    """'Stripe, Inc.' and 'stripe' compare equal."""
    base = re.sub(
        r"\b(inc|ltd|llc|corp|corporation|co|company|limited|plc|the)\b\.?", " ", text.lower()
    )
    return "".join(re.findall(r"[a-z0-9]+", base))


def names_match(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    return (
        bool(ka)
        and bool(kb)
        and (ka == kb or (min(len(ka), len(kb)) >= 5 and (ka.startswith(kb) or kb.startswith(ka))))
    )


def _greenhouse_name(client: httpx.Client, slug: str) -> str:
    try:
        data = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}").json()
        return str(data.get("name", "")) if isinstance(data, dict) else ""
    except (httpx.HTTPError, ValueError):
        return ""


def probe_slugs(client: httpx.Client, slugs: list[str]) -> list[Hit]:
    """Public board APIs that answer with at least one job for any of these slugs."""
    hits: list[Hit] = []
    for slug in slugs:
        for ats, template in PROBES.items():
            try:
                resp = client.get(template.format(slug=slug))
                if resp.status_code != 200:
                    continue
                data = resp.json()
            except (httpx.HTTPError, ValueError):
                continue
            jobs = data.get("jobs", []) if isinstance(data, dict) else data
            if jobs:
                ids = frozenset(str(j.get("id", "")) for j in jobs if isinstance(j, dict))
                name = _greenhouse_name(client, slug) if ats == "greenhouse" else ""
                hits.append(Hit(ats, slug, ids, name))
    return hits
