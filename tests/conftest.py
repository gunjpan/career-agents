import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobagent.models.company import Company
from jobagent.models.criteria import Criteria, load_criteria
from jobagent.models.job import RawPosting
from jobagent.models.tailoring import TailoringConfig, load_tailoring_config

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 6, tzinfo=UTC)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def real_criteria() -> Criteria:
    """The committed config/criteria.example.yaml: generic and identical on every machine.
    (The private criteria.local.yaml is deliberately never read by the tests.)"""
    return load_criteria(Path(__file__).parents[1] / "config" / "criteria.example.yaml")


@pytest.fixture
def criteria(real_criteria: Criteria) -> Criteria:
    return real_criteria


@pytest.fixture
def cfg() -> TailoringConfig:
    """The real config/tailoring.yaml (Tailor, Verifier, limits and the keep setting)."""
    return load_tailoring_config()


def fixture_client(name: str) -> httpx.Client:
    """An httpx client that answers every request with a recorded API response."""
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data)))


def company(name: str = "Acme", ats: str = "ashby", board_id: str = "acme") -> Company:
    return Company(name=name, ats=ats, board_id=board_id, status="active")


def posting(**kw) -> RawPosting:
    base = {
        "company": "Acme",
        "ats": "ashby",
        "external_id": "1",
        "title": "Director of Engineering",
        "locations": ["Toronto, ON"],
        "url": "https://example.com/1",
        "posted_at": datetime(2026, 9, 20, tzinfo=UTC),
    }
    return RawPosting(**{**base, **kw})


# --- public-repo safety helpers ------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b")
PRIVATE_TERMS_FILE = (
    Path(__file__).parents[1] / "config" / "private_terms.txt"
)  # git-ignored, one term per line


def private_terms() -> list[str]:
    """Your own identifiers and targets, kept OUT of the repo so the guards do not leak them."""
    if not PRIVATE_TERMS_FILE.exists():
        return []
    return [
        t.strip().lower()
        for t in PRIVATE_TERMS_FILE.read_text().splitlines()
        if t.strip() and not t.startswith("#")
    ]


def assert_public_safe(text: str, where: str) -> None:
    """No real email address or phone number, and none of the private terms, anywhere in `text`."""
    emails = {
        e
        for e in _EMAIL.findall(text)
        if not e.lower().endswith(("@example.com", "@example.org", "@example.invalid"))
    }
    assert not emails, f"{where}: contains email addresses {sorted(emails)}"
    assert not _PHONE.search(text), f"{where}: contains a phone number"
    for term in private_terms():
        assert term not in text.lower(), f"{where}: contains a private term"
