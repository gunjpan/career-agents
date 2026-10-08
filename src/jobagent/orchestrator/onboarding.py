from collections.abc import Callable

from jobagent.models.company import COMPANIES_HEADERS, Company
from jobagent.models.onboarding import OnboardingResult
from jobagent.onboarding.onboard import Onboarder
from jobagent.storage.base import Storage


def add_company(storage: Storage, name: str, tier: str, levels: list[str]) -> str:
    """Adding a company = one row with status `pending`. No code change, no platform details."""
    storage.ensure_headers("Companies", COMPANIES_HEADERS)
    name = name.strip()
    if any(
        r.get("name", "").strip().lower() == name.lower() for r in storage.read_records("Companies")
    ):
        return f"not added: {name} is already in the Companies tab"
    row = Company(name=name, tier=tier, levels=levels, status="pending")
    storage.append_records("Companies", COMPANIES_HEADERS, [row.to_record()])
    return f"added {name} (pending)"


def _row_update(company: Company, result: OnboardingResult) -> dict[str, str]:
    notes = result.reason + (f" [{'; '.join(result.evidence)}]" if result.evidence else "")
    update = {"status": result.status, "notes": notes[:400]}
    if result.ats and result.board_id:  # never blank out what the user (or an earlier run) set
        update |= {"ats": result.ats, "board_id": result.board_id}
    return update


def run_onboarding(
    storage: Storage,
    onboarder: Onboarder,
    *,
    names: set[str] | None = None,
    dry_run: bool = False,
    on_progress: Callable[[int, int, str], None] = lambda done, total, name: None,
) -> list[tuple[Company, OnboardingResult]]:
    """Onboard every `pending` company (or the named ones, whatever their status except active)."""
    storage.ensure_headers("Companies", COMPANIES_HEADERS)
    wanted = {n.lower() for n in names} if names else None
    todo = [
        c
        for c in (
            Company.from_record(r) for r in storage.read_records("Companies") if r.get("name")
        )
        if (c.name.lower() in wanted if wanted else c.status == "pending")
    ]
    results: list[tuple[Company, OnboardingResult]] = []
    for i, company in enumerate(todo):
        on_progress(i, len(todo), company.name)
        result = onboarder.onboard(company)
        results.append((company, result))
        if not dry_run:
            storage.update_records(
                "Companies", "name", {company.name: _row_update(company, result)}
            )
    return results
