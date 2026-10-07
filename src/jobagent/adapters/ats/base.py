import html
import re
from typing import Protocol

import httpx

from jobagent.models.company import Company
from jobagent.models.job import RawPosting

USER_AGENT = "jobagent/0.1 (personal job search tool)"


class ATSAdapter(Protocol):
    """One class per ATS. Callers only see this interface."""

    name: str

    def fetch(self, company: Company) -> list[RawPosting]: ...


def make_client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(p|div|li|h[1-6]|ul|ol|br)>|<br\s*/?>", re.IGNORECASE)


def html_to_text(raw: str) -> str:
    """HTML (possibly entity-escaped, as Greenhouse sends it) -> plain text."""
    text = html.unescape(raw) if "&lt;" in raw else raw
    text = _BLOCK_END.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()
