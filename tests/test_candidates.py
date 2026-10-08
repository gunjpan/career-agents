from io import StringIO

import pytest
from rich.console import Console

from jobagent.documents.diff import build_diff, render_html, render_terminal, word_diff
from jobagent.models.candidates import CandidateRecord, Manifest
from jobagent.models.tailoring import TailoredBullet, TailoredRole
from jobagent.orchestrator.tailoring import _candidate_record, pick_candidate, retailor

from .test_tailoring import (
    MASTER,
    FakeProvider,
    FakeStore,
    good_output,
    job,
    only_row,
    run,
    storage_with,
    verdict,
)


def tailor_once(storage, drive, cfg, **kw):
    return run(
        storage, FakeProvider([kw.get("output") or good_output()], [verdict("traced")]), cfg, drive
    )


def two_candidates(tmp_path, cfg):
    storage, drive = storage_with(tmp_path, job()), FakeStore()
    tailor_once(storage, drive, cfg)
    retailor(storage, ["j1"])
    tailor_once(storage, drive, cfg)
    return storage, drive


# --- saving candidates ---------------------------------------------------------------------


def test_each_run_adds_a_candidate_folder_inside_the_same_job_folder(tmp_path, cfg):
    storage, drive = two_candidates(tmp_path, cfg)

    row = only_row(storage)
    manifest = Manifest.from_cell(row["tailor_candidates"])
    assert [c.n for c in manifest.candidates] == [1, 2] and manifest.full
    assert list(drive.folders.values()) == [
        "Acme - Director, Platform - j1",
        "candidate-1",
        "candidate-2",
    ]
    assert row["final_candidate"] == ""  # two versions: the user has to pick
    assert row["drive_url"] == manifest.candidates[1].folder_url and row["state"] == "ready"


def test_the_saved_record_carries_the_master_text_it_was_built_from(tmp_path, cfg):
    storage, drive = storage_with(tmp_path, job()), FakeStore()
    tailor_once(storage, drive, cfg)

    meta = Manifest.from_cell(only_row(storage)["tailor_candidates"]).candidates[0]
    record = CandidateRecord.model_validate_json(drive.download_bytes(meta.json_file_id))
    assert (
        record.master_text["d-1"] == "Led 34+ engineers across four platforms with a $15MM+ budget."
    )
    assert (
        "summary" in record.master_text
        and record.role_labels["acme-dir"] == "Director, Engineering (Acme)"
    )
    assert {b.source_id for r in record.output.roles for b in r.bullets} >= {"d-1", "m-1", "ai-1"}
    assert meta.keep == "all_bullets" and meta.tailor_prompt_version == 1


def test_a_third_run_is_refused_until_one_candidate_is_picked(tmp_path, cfg):
    storage, drive = two_candidates(tmp_path, cfg)
    assert retailor(storage, ["j1"])["j1"].startswith("not changed: two candidates")

    # even if the job somehow reaches `approved` again, tailoring refuses to pile up a third version
    storage.update_records("Jobs", "job_id", {"j1": {"state": "approved"}})
    provider = FakeProvider()
    summary, _ = run(storage, provider, cfg, drive)
    assert summary.outcomes[0].status == "waiting" and provider.calls == []  # no model spend


# --- pick ----------------------------------------------------------------------------------


def test_pick_keeps_one_candidate_and_trashes_the_other(tmp_path, cfg):
    storage, drive = two_candidates(tmp_path, cfg)
    before = Manifest.from_cell(only_row(storage)["tailor_candidates"])

    message = pick_candidate(storage, drive, "j1", 1)

    row = only_row(storage)
    after = Manifest.from_cell(row["tailor_candidates"])
    assert message.startswith("picked candidate 1")
    assert [c.n for c in after.candidates] == [1] and row["final_candidate"] == "1"
    assert drive.trashed == [before.candidates[1].folder_id]  # only the loser; trash, not delete
    assert row["drive_url"] == before.candidates[0].folder_url and row["state"] == "ready"


def test_after_a_pick_a_new_retailor_compares_against_the_kept_version(tmp_path, cfg):
    storage, drive = two_candidates(tmp_path, cfg)
    pick_candidate(storage, drive, "j1", 2)
    assert retailor(storage, ["j1"])["j1"].startswith("approved again")
    tailor_once(storage, drive, cfg)

    manifest = Manifest.from_cell(only_row(storage)["tailor_candidates"])
    assert [c.n for c in manifest.candidates] == [2, 3]  # numbers are never reused
    assert only_row(storage)["final_candidate"] == ""


def test_pick_rejects_an_unknown_candidate_and_trashes_nothing(tmp_path, cfg):
    storage, drive = two_candidates(tmp_path, cfg)
    assert pick_candidate(storage, drive, "j1", 7).startswith("not picked: no candidate 7")
    assert pick_candidate(storage, drive, "ghost", 1) == "not found"
    assert (
        drive.trashed == []
        and len(Manifest.from_cell(only_row(storage)["tailor_candidates"]).candidates) == 2
    )


def test_picking_the_only_candidate_trashes_nothing(tmp_path, cfg):
    storage, drive = storage_with(tmp_path, job()), FakeStore()
    tailor_once(storage, drive, cfg)
    assert pick_candidate(storage, drive, "j1", 1).startswith("picked candidate 1")
    assert drive.trashed == []


# --- diff ----------------------------------------------------------------------------------


def record(output) -> CandidateRecord:
    return _candidate_record(job(), output, MASTER)


def reworded_director() -> object:
    return good_output(roles=[
        TailoredRole(role_id="acme-dir", bullets=[
            TailoredBullet(source_id="d-1", text="Led 34+ engineers across four client platforms with a $15MM+ budget."),
            TailoredBullet(source_id="d-2", text="Sustained 99.95% uptime on the trading platform."),  # master wording
        ]),
        TailoredRole(role_id="acme-mgr", bullets=[TailoredBullet(source_id="m-1", text="Led a 20-person team delivering mobile and API products.")]),
    ])  # fmt: skip


def test_word_diff_marks_added_and_removed_words():
    pieces = word_diff(
        "Led 34+ engineers across four platforms", "Led 34+ engineers across four client platforms"
    )
    assert [(p.op, p.text) for p in pieces] == [
        ("same", "Led 34+ engineers across four"),
        ("add", "client"),
        ("same", "platforms"),
    ]
    assert [p.op for p in word_diff("a b c", "a c")] == ["same", "del", "same"]
    assert [p.op for p in word_diff("same words", "same words")] == ["same"]


def test_diff_statuses_distinguish_reworded_unchanged_and_not_included():
    view = build_diff({1: record(reworded_director()), 2: record(good_output())})
    bullets = {b.source_id: b for r in view.roles for b in r.bullets}

    assert bullets["d-1"].status == {1: "reworded", 2: "reworded"}  # both differ from the master
    assert bullets["d-2"].status == {1: "unchanged", 2: "unchanged"}
    assert bullets["d-3"].status == {1: "absent", 2: "absent"} if "d-3" in bullets else True
    assert view.counts(1)[0] >= 1 and view.versions == [1, 2]
    assert {r.role_id for r in view.roles} >= {"acme-dir", "acme-mgr"}


def test_a_bullet_one_candidate_omits_shows_as_absent_in_that_candidate_only():
    view = build_diff({1: record(good_output()), 2: record(reworded_director())})
    bullets = {b.source_id: b for r in view.roles for b in r.bullets}
    assert bullets["ai-1"].status == {
        1: "unchanged",
        2: "absent",
    }  # candidate 2 left the AI bullet out


def test_terminal_diff_names_the_changes_and_compares_the_two_candidates():
    view = build_diff({1: record(reworded_director()), 2: record(good_output())})
    console = Console(file=StringIO(), width=140, record=True)
    render_terminal(view, console)
    text = console.export_text()
    assert "master vs candidate 1 vs candidate 2" in text and "bullets reworded" in text
    assert "d-1" in text and "client" in text and "candidate 1 -> 2" in text
    assert "unchanged in every candidate" in text  # unchanged bullets are collapsed, not repeated


def test_html_diff_is_side_by_side_escaped_and_highlights_changes():
    out = reworded_director()
    out = out.model_copy(
        update={
            "summary": [
                out.summary[0].model_copy(
                    update={"text": "Leader <b>with</b> 13 years of experience."}
                )
            ]
        }
    )
    html = render_html(build_diff({1: record(out), 2: record(good_output())}))
    assert "<th>Candidate 1</th><th>Candidate 2</th>" in html and "<ins>client</ins>" in html
    assert "&lt;b&gt;" in html and "<b>with</b>" not in html  # model text can never inject markup
    assert "Director, Engineering (Acme)" in html


def test_a_single_candidate_diff_compares_with_the_master_only():
    view = build_diff({3: record(reworded_director())})
    console = Console(file=StringIO(), width=140, record=True)
    render_terminal(view, console)
    assert view.versions == [3] and "candidate 3" in console.export_text()
    assert "candidate 1 -> 2" not in console.export_text()


# --- running total cost --------------------------------------------------------------------

ONE_RUN = 2 * (1000 * 2 + 500 * 10) / 1e6  # one Tailor + one Verifier call in the fake provider


def total(storage) -> float:
    return float(only_row(storage)["tailor_total_cost_usd"])


def test_total_cost_adds_up_across_runs_while_the_run_cost_shows_only_the_latest(tmp_path, cfg):
    storage, drive = storage_with(tmp_path, job()), FakeStore()
    first, _ = tailor_once(storage, drive, cfg)
    assert total(storage) == pytest.approx(ONE_RUN) and first.outcomes[
        0
    ].total_cost_usd == pytest.approx(ONE_RUN)

    retailor(storage, ["j1"])
    second, _ = tailor_once(storage, drive, cfg)

    row = only_row(storage)
    assert float(row["tailor_cost_usd"]) == pytest.approx(ONE_RUN)  # this run only
    assert total(storage) == pytest.approx(2 * ONE_RUN)  # lifetime
    assert second.outcomes[0].cost_usd == pytest.approx(ONE_RUN) and second.outcomes[
        0
    ].total_cost_usd == pytest.approx(2 * ONE_RUN)


def test_a_blocked_run_still_counts_because_it_cost_money(tmp_path, cfg):
    storage, drive = storage_with(tmp_path, job()), FakeStore()
    run(
        storage,
        FakeProvider([good_output(), good_output()], [verdict("unsupported")] * 2),
        cfg,
        drive,
    )
    assert only_row(storage)["tailor_status"] == "blocked" and total(storage) == pytest.approx(
        4 * ONE_RUN / 2
    )

    retailor(storage, ["j1"])
    tailor_once(storage, drive, cfg)
    assert total(storage) == pytest.approx(
        2 * ONE_RUN + ONE_RUN
    )  # blocked (2 attempts) + the passing run


def test_retailor_and_pick_never_reduce_the_total(tmp_path, cfg):
    storage, drive = two_candidates(tmp_path, cfg)
    before = total(storage)
    pick_candidate(storage, drive, "j1", 1)  # trashes candidate 2 and drops it from the manifest
    assert total(storage) == pytest.approx(before) == pytest.approx(2 * ONE_RUN)
    retailor(storage, ["j1"])
    assert total(storage) == pytest.approx(before) and only_row(storage)["tailor_cost_usd"] == ""


def test_rows_from_before_the_total_existed_start_from_their_recorded_candidates(tmp_path, cfg):
    storage, drive = storage_with(tmp_path, job()), FakeStore()
    tailor_once(storage, drive, cfg)
    storage.update_records("Jobs", "job_id", {"j1": {"tailor_total_cost_usd": ""}})  # an old row
    retailor(storage, ["j1"])
    tailor_once(storage, drive, cfg)
    assert total(storage) == pytest.approx(2 * ONE_RUN)  # candidate 1's cost + the new run
