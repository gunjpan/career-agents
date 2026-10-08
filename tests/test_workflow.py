"""Guards for a PUBLIC repo's workflow: who can trigger it, what it can touch, what it prints."""

import re
from pathlib import Path

import pytest
import yaml

from jobagent.settings import Settings

PATH = Path(__file__).parents[1] / ".github" / "workflows" / "daily.yml"


@pytest.fixture(scope="module")
def wf() -> dict:
    return yaml.safe_load(PATH.read_text())


def steps(wf):
    return [s for job in wf["jobs"].values() for s in job["steps"]]


def test_only_scheduled_and_manual_triggers_never_pull_requests_or_pushes(wf):
    triggers = wf.get(True) or wf.get("on")  # YAML 1.1 reads the key `on` as the boolean True
    assert set(triggers) == {
        "schedule",
        "workflow_dispatch",
    }  # no pull_request, pull_request_target, push


def test_the_cron_is_a_valid_five_field_expression(wf):
    [entry] = (wf.get(True) or wf.get("on"))["schedule"]
    fields = entry["cron"].split()
    assert len(fields) == 5 and all(re.fullmatch(r"[\d*/,\-]+", f) for f in fields)


def test_the_token_is_read_only_and_runs_cannot_overlap_or_hang(wf):
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["cancel-in-progress"] is False
    for job in wf["jobs"].values():
        assert 0 < job["timeout-minutes"] <= 60


def test_every_third_party_action_is_pinned_to_a_full_commit_sha(wf):
    uses = [s["uses"] for s in steps(wf) if "uses" in s]
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_checkout_does_not_leave_credentials_behind(wf):
    checkout = next(s for s in steps(wf) if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False


def test_every_secret_it_passes_is_one_the_app_actually_reads(wf):
    fields = set(Settings.model_fields)
    used = {m for s in steps(wf) for m in re.findall(r"secrets\.(\w+)", yaml.safe_dump(s))}
    assert used, "expected the workflow to pass secrets"
    assert {name.lower() for name in used} <= fields, used - {f.upper() for f in fields}


def test_secrets_are_only_given_to_the_step_that_needs_them(wf):
    with_secrets = [s for s in steps(wf) if "secrets." in yaml.safe_dump(s)]
    assert [s["run"] for s in with_secrets] == ["uv run jobagent daily"]


def test_no_step_can_print_the_environment_or_a_secret(wf):
    for s in steps(wf):
        script = s.get("run", "")
        assert not re.search(r"\b(printenv|env\b|set -x|echo .*secrets\.|cat .*resume)", script), (
            script
        )


def test_the_command_it_runs_exists_and_the_resume_never_touches_the_repo():
    from typer.testing import CliRunner

    from jobagent.cli import app

    assert "daily" in CliRunner().invoke(app, ["--help"]).output
    ignored = Path(__file__).parents[1].joinpath(".gitignore").read_text()
    assert "master_resume.yaml" in ignored and ".env" in ignored


def test_the_package_cache_is_off_because_it_is_an_attack_surface_on_a_public_repo(wf):
    setup = next(s for s in steps(wf) if s.get("uses", "").startswith("astral-sh/setup-uv"))
    assert setup["with"]["enable-cache"] is False  # the action's default is 'auto', which caches


# --- the test workflow may run on pull requests, so it must be incapable of leaking anything -------

TESTS = Path(__file__).parents[1] / ".github" / "workflows" / "tests.yml"


@pytest.fixture(scope="module")
def tests_wf() -> dict:
    return yaml.safe_load(TESTS.read_text())


def test_the_test_workflow_uses_no_secrets_and_a_read_only_token(tests_wf):
    assert (
        "secrets."
        not in TESTS.read_text()
        .replace("NO secrets", "")
        .replace("uses no secrets", "")
        .split("jobs:")[1]
    )
    assert tests_wf["permissions"] == {"contents": "read"}
    assert "env" not in tests_wf and all("env" not in s for s in steps(tests_wf))


def test_the_test_workflow_never_uses_the_dangerous_pull_request_target_trigger(tests_wf):
    triggers = tests_wf.get(True) or tests_wf.get("on")
    assert set(triggers) == {
        "push",
        "pull_request",
    }  # pull_request_target would run PR code WITH secrets
    assert triggers["push"]["branches"] == ["main"]


def test_the_test_workflow_pins_actions_and_has_no_cache_and_a_timeout(tests_wf):
    uses = [s["uses"] for s in steps(tests_wf) if "uses" in s]
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses
    setup = next(s for s in steps(tests_wf) if s.get("uses", "").startswith("astral-sh/setup-uv"))
    assert setup["with"]["enable-cache"] is False
    assert all(0 < j["timeout-minutes"] <= 30 for j in tests_wf["jobs"].values())
