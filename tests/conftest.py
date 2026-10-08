import json
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
