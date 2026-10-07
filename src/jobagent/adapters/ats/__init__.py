import httpx

from jobagent.adapters.ats.ashby import AshbyAdapter
from jobagent.adapters.ats.base import ATSAdapter
from jobagent.adapters.ats.greenhouse import GreenhouseAdapter
from jobagent.adapters.ats.lever import LeverAdapter
from jobagent.adapters.ats.workday import WorkdayAdapter
from jobagent.models.criteria import DiscoveryCriteria


def build_adapters(client: httpx.Client, discovery: DiscoveryCriteria) -> dict[str, ATSAdapter]:
    """Adding an ATS = one adapter class + one line here."""
    return {
        "ashby": AshbyAdapter(client),
        "greenhouse": GreenhouseAdapter(client),
        "lever": LeverAdapter(client),
        "workday": WorkdayAdapter(client, discovery),
    }
