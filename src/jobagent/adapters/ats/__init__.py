import httpx

from jobagent.adapters.ats.ashby import AshbyAdapter
from jobagent.adapters.ats.base import ATSAdapter
from jobagent.adapters.ats.greenhouse import GreenhouseAdapter
from jobagent.adapters.ats.lever import LeverAdapter

ADAPTER_CLASSES = {
    cls.name: cls for cls in (AshbyAdapter, GreenhouseAdapter, LeverAdapter)
}  # adding an ATS = one class + one entry here


def build_adapters(client: httpx.Client) -> dict[str, ATSAdapter]:
    return {name: cls(client) for name, cls in ADAPTER_CLASSES.items()}
