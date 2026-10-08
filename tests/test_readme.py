"""The README and setup guide are the project's front door: keep them true."""

import re
from pathlib import Path

import pytest

from jobagent.cli import app

ROOT = Path(__file__).parents[1]
DOCS = [ROOT / "README.md", ROOT / "docs" / "setup.md"]
COMMANDS = {
    c.name or c.callback.__name__.replace("_cmd", "").replace("_", "-")
    for c in app.registered_commands
}


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_jobagent_command_the_docs_mention_exists(doc):
    mentioned = set(re.findall(r"jobagent ([a-z][a-z-]+)", doc.read_text()))
    mentioned -= {"is", "the", "daily"}  # prose, and the workflow's own command is checked below
    assert mentioned, doc
    unknown = mentioned - COMMANDS
    assert not unknown, f"{doc.name} mentions commands that do not exist: {sorted(unknown)}"


def test_the_daily_command_the_docs_describe_exists():
    assert "daily" in COMMANDS and "demo" in COMMANDS


def test_the_files_the_readme_links_to_exist():
    text = (ROOT / "README.md").read_text()
    for target in re.findall(r"\]\(((?!http)[^)#]+)\)", text):
        assert (ROOT / target).exists(), target


def test_the_config_files_the_docs_tell_you_to_copy_exist():
    for doc in DOCS:
        for src in re.findall(r"cp (config/[\w./-]+\.example\.yaml)", doc.read_text()):
            assert (ROOT / src).exists(), src


def test_the_readme_leaks_no_personal_contact_details_or_private_terms():
    from .conftest import assert_public_safe

    for doc in DOCS:
        assert_public_safe(
            doc.read_text(), doc.name
        )  # real emails/phones, plus config/private_terms.txt if present


def test_the_readme_does_not_credit_the_tools_used_to_write_it():
    assert (
        "claude code" not in (ROOT / "README.md").read_text().lower()
    )  # the owner's choice, kept out on purpose


def test_the_eval_numbers_in_the_readme_match_the_committed_dataset_sizes():
    from jobagent.evals.models import load_eval_config, load_scorer_cases

    cases = load_scorer_cases(load_eval_config().scorer_cases)
    assert len(cases) == 13  # the README reports 8/9 then 13/13; update both when the set changes
    assert "13/13" in (ROOT / "README.md").read_text()
