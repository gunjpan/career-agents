import json
import socket
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from jobagent.cli import app
from jobagent.demo import scenario
from jobagent.demo.replay import (
    RECORDING,
    RecordingProvider,
    ReplayMiss,
    ReplayProvider,
    request_key,
)
from jobagent.demo.scenario import run_demo
from jobagent.demo.store import LocalArtifactStore
from jobagent.llm.base import LLMResult, Usage
from jobagent.models.criteria import EXAMPLE_CRITERIA
from jobagent.models.tailoring import Claim

# --- the recorded demo runs the real pipeline, offline ----------------------------------------


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """Run the whole demo from the committed recording. Any network use would fail the test."""

    def no_network(*args, **kwargs):
        raise AssertionError("the demo must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    console = Console(file=StringIO(), width=140, record=True)
    replay = ReplayProvider()
    result = run_demo(console, replay, tmp_path)
    return result, console.export_text(), replay, tmp_path


def test_the_walkthrough_goes_from_six_postings_to_a_verified_resume(demo):
    result, _, _, _ = demo
    assert (result.companies, result.fetched, result.filtered_out) == (
        6,
        6,
        2,
    )  # AVP and Principal are filtered
    assert result.scored == 4 and result.shortlisted >= 1
    assert result.approved and result.tailor_status == "ready" and result.attempts >= 1
    assert (
        result.claims_checked >= 5 and result.model_calls == 6
    )  # 4 scores + 1 tailor + 1 verifier, all replayed


def test_the_files_really_exist_and_the_pipeline_never_leaves_the_working_folder(demo):
    result, _, _, tmp_path = demo
    assert {"resume.docx", "resume.pdf", "tailored.json", "verifier_report.md"} <= set(result.files)
    for name in result.files:
        found = list(tmp_path.rglob(name))
        assert found and found[0].stat().st_size > 0


def test_an_injected_fabrication_is_stopped_by_plain_code(demo):
    result, text, _, _ = demo
    assert result.fabrication_caught
    assert "Stopped by plain code, before any model call" in text and "across 40 markets" in text


def test_the_screen_says_what_is_real_and_what_is_the_users_job(demo):
    _, text, _, _ = demo
    for phrase in (
        "recorded",
        "no network, no API keys",
        "only a human can approve",
        "plays your part",
        "nothing",
    ):
        assert phrase.lower() in text.lower(), phrase
    assert "Submitted anywhere" in text


def test_it_uses_the_public_example_rules_never_a_private_criteria_file(tmp_path, monkeypatch):
    seen: list = []
    real = scenario.load_criteria
    monkeypatch.setattr(
        scenario,
        "load_criteria",
        lambda path=None, settings=None: (seen.append(path), real(path, settings))[1],
    )
    run_demo(Console(file=StringIO(), width=140), ReplayProvider(), tmp_path)
    assert seen == [EXAMPLE_CRITERIA]


def test_the_cli_command_runs_to_the_end_with_no_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = CliRunner().invoke(app, ["demo", "--fast"])
    assert out.exit_code == 0, out.output
    assert "Result" in out.output and "Submitted anywhere" in out.output


# --- the recording itself ------------------------------------------------------------------------


def test_the_committed_recording_is_complete_and_free_of_private_data():
    from .conftest import assert_public_safe

    data = json.loads(RECORDING.read_text())
    assert len(data["responses"]) == 6 and data["meta"]["recorded_at"] and data["meta"]["models"]
    assert_public_safe(RECORDING.read_text(), RECORDING.name)


def test_a_changed_prompt_no_longer_matches_the_recording_so_the_demo_says_so(demo):
    _, _, replay, _ = demo
    with pytest.raises(ReplayMiss, match="jobagent demo --record"):
        replay.complete(
            model="m",
            cacheable_prefix="a different instruction set",
            prompt="p",
            schema=Claim,
            max_tokens=10,
        )


def test_the_request_key_depends_on_everything_the_model_was_sent():
    base = request_key(Claim, "m", "prefix", "prompt")
    assert base == request_key(Claim, "m", "prefix", "prompt")
    for changed in (request_key(Claim, "m2", "prefix", "prompt"), request_key(Claim, "m", "prefix2", "prompt"), request_key(Claim, "m", "prefix", "prompt2")):  # fmt: skip
        assert changed != base


def test_recording_then_replaying_returns_the_same_answer_and_usage(tmp_path):
    class Fake:
        public_data_only = False

        def complete(self, **kw):
            return LLMResult(Claim(text="hello", supports=["a"]), Usage(11, 22, 3, 4), "model-x")

    recorder = RecordingProvider(Fake())
    recorder.complete(model="model-x", cacheable_prefix="P", prompt="Q", schema=Claim, max_tokens=5)
    path = tmp_path / "rec.json"
    assert recorder.save(path) == 1

    replayed = ReplayProvider(path).complete(
        model="model-x", cacheable_prefix="P", prompt="Q", schema=Claim, max_tokens=5
    )
    assert replayed.parsed == Claim(text="hello", supports=["a"]) and replayed.usage == Usage(
        11, 22, 3, 4
    )
    assert replayed.model == "model-x"


def test_the_replay_provider_may_serve_the_agents_that_see_the_resume():
    assert ReplayProvider().public_data_only is False  # it is a recording, not a free-tier API


# --- the local stand-in for Drive ------------------------------------------------------------------


def test_the_local_store_creates_uploads_downloads_and_trashes(tmp_path):
    store = LocalArtifactStore(tmp_path)
    job, url = store.create_job_folder("Acme: Director / 1")  # unsafe characters are cleaned
    cand, _ = store.create_subfolder("candidate-1", job)
    file_id = store.upload_bytes("resume.pdf", b"%PDF", "application/pdf", cand)
    assert store.download_bytes(file_id) == b"%PDF" and url.startswith("file://")
    store.trash(cand)
    assert not Path(cand).exists() and (tmp_path / ".trash" / "candidate-1").exists()
