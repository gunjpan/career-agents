from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from jobagent.agents.context import load_yaml
from jobagent.agents.prompts import Prompt
from jobagent.agents.scorer import Scorer
from jobagent.evals.models import (
    LEVELS,
    load_eval_config,
    load_scorer_cases,
    load_verifier_cases,
)
from jobagent.evals.report import EVALS_HEADERS, EvalReport, Metric, log_report
from jobagent.evals.scorer_eval import distance, run_scorer_eval
from jobagent.evals.verifier_eval import run_verifier_eval
from jobagent.models.scoring import load_scoring_config
from jobagent.models.tailoring import Limits, load_tailoring_config
from jobagent.orchestrator.checks import check_tailor_output
from jobagent.storage.csv_store import CsvStorage

from .test_scoring import FakeProvider as ScorerProvider
from .test_scoring import output
from .test_tailoring import FakeProvider as TailorProvider
from .test_tailoring import agents, verdict

CONFIG = load_eval_config()
SCORER_CASES = load_scorer_cases(CONFIG.scorer_cases)
VERIFIER_CASES = load_verifier_cases(CONFIG.verifier_cases)
MASTER = load_yaml(CONFIG.master_resume)


# --- the datasets are what we claim they are ---------------------------------------------------


def test_there_are_at_least_fifteen_cases_with_unique_ids_and_a_held_out_split():
    assert len(SCORER_CASES) + len(VERIFIER_CASES) >= 15 and len(VERIFIER_CASES) == 6
    ids = [c.id for c in [*SCORER_CASES, *VERIFIER_CASES]]
    assert len(set(ids)) == len(ids)
    assert (
        sum(c.split == "held_out" for c in SCORER_CASES) >= 3
        and sum(c.split == "dev" for c in SCORER_CASES) == 9
    )


def test_every_target_level_is_covered_and_most_cases_are_traps():
    labels = {c.label_level for c in SCORER_CASES}
    assert {"senior_manager", "director", "vp"} <= labels and {
        "individual_contributor",
        "manager",
    } <= labels
    traps = [c for c in SCORER_CASES if not c.title_matches_level]
    assert len(traps) >= 5  # most cases are deliberate title-vs-scope traps


def test_the_eval_data_is_fictional_and_public_safe():
    blob = " ".join(
        Path(p).read_text().lower()
        for p in (CONFIG.scorer_cases, CONFIG.verifier_cases, CONFIG.master_resume)
    )
    for real in (
        "wealthsimple",
        "royal bank",
        "rbc",
        "bmo",
        "cibc",
        "scotiabank",
        "gunjan",
        "pandya",
        "td bank",
    ):
        assert real not in blob, real


def test_clean_drafts_really_are_clean_under_the_code_checks():
    for case in (c for c in VERIFIER_CASES if c.kind == "clean"):
        assert (
            check_tailor_output(case.draft, MASTER, Limits(), enforce_bullet_limits=False) == []
        ), case.id


def test_code_targeted_corruptions_are_really_caught_by_code():
    for case in (c for c in VERIFIER_CASES if c.expected_layer == "code"):
        assert check_tailor_output(case.draft, MASTER, Limits(), enforce_bullet_limits=False), (
            case.id
        )


def test_verifier_targeted_corruptions_get_past_the_code_checks():
    """Otherwise the case would test nothing: it must reach the model to be judged."""
    cases = [c for c in VERIFIER_CASES if c.expected_layer == "verifier"]
    assert len(cases) >= 3
    for case in cases:
        assert (
            check_tailor_output(case.draft, MASTER, Limits(), enforce_bullet_limits=False) == []
        ), case.id


def test_layer_labels_agree_with_case_kinds():
    for c in VERIFIER_CASES:
        assert (c.kind == "clean") == (c.expected_layer == "none"), c.id


# --- scorer eval metrics (fake model, no cost) ------------------------------------------------


DEV = [
    c for c in SCORER_CASES if c.split == "dev"
]  # the original nine; thresholds below assume 9 cases


def make_scorer(levels: list[str], flags: list[bool] | None = None, cases=None):
    flags = flags or [c.title_matches_level for c in (cases or SCORER_CASES)]
    outs = [
        output(real_level=lv, title_matches_level=f) for lv, f in zip(levels, flags, strict=True)
    ]
    return Scorer(ScorerProvider(outs), load_scoring_config(), Prompt(7, "P"), "resume", "profiles")


def perfect(cases=None) -> list[str]:
    return [c.label_level for c in (cases or SCORER_CASES)]


def test_a_perfect_scorer_meets_every_target():
    report = run_scorer_eval(make_scorer(perfect()), SCORER_CASES, CONFIG.targets)
    by = {m.name: m for m in report.metrics}
    assert by["scorer_level_accuracy"].value == 1.0 and report.passed and report.failures == []
    assert (report.model, report.prompt_version, report.cases) == (
        "claude-haiku-4-5",
        7,
        len(SCORER_CASES),
    )


def test_one_miss_in_nine_fails_the_ninety_percent_target_and_names_the_case():
    levels = perfect(DEV)
    levels[3] = "senior_manager"  # the Principal Engineer (an IC) rated two levels too high
    report = run_scorer_eval(make_scorer(levels, cases=DEV), DEV, CONFIG.targets)
    by = {m.name: m for m in report.metrics}
    assert (
        by["scorer_level_accuracy"].value == pytest.approx(8 / 9)
        and by["scorer_level_accuracy"].passed is False
    )
    assert not report.passed and any(
        "s4-principal-engineer" in f and "got senior_manager" in f for f in report.failures
    )
    assert "individual_contributor->senior_manager x1" in report.notes


def test_within_one_level_tolerates_adjacent_misses_but_not_two_away():
    near = perfect(DEV)
    near[2] = "senior_manager"  # manager -> senior_manager: one level off
    by = {
        m.name: m
        for m in run_scorer_eval(make_scorer(near, cases=DEV), DEV, CONFIG.targets).metrics
    }
    assert (
        by["scorer_within_one_level"].passed is True and by["scorer_level_accuracy"].passed is False
    )
    far = perfect(DEV)
    far[3] = "director"  # individual_contributor -> director: three levels off
    report = run_scorer_eval(make_scorer(far, cases=DEV), DEV, CONFIG.targets)
    assert {m.name: m for m in report.metrics}["scorer_within_one_level"].passed is False
    assert distance("manager", "vp") == 3 and distance("vp", "vp") == 0
    assert set(LEVELS) >= {c.label_level for c in SCORER_CASES}


def test_title_mismatch_detection_is_reported_but_not_a_pass_fail_target():
    wrong_flags = [not c.title_matches_level for c in SCORER_CASES]
    report = run_scorer_eval(make_scorer(perfect(), wrong_flags), SCORER_CASES, CONFIG.targets)
    by = {m.name: m for m in report.metrics}
    assert (
        by["title_mismatch_flag_accuracy"].value == 0
        and by["title_mismatch_flag_accuracy"].passed is None
        and report.passed
    )


def test_repeats_measure_run_to_run_agreement():
    cases = SCORER_CASES[:2]
    outs = [output(real_level=cases[0].label_level), output(real_level="vp"),  # case 1: two different answers
            output(real_level=cases[1].label_level), output(real_level=cases[1].label_level)]  # fmt: skip
    scorer = Scorer(ScorerProvider(outs), load_scoring_config(), Prompt(1, "P"), "r", "p")
    report = run_scorer_eval(scorer, cases, CONFIG.targets, repeats=2)
    by = {m.name: m for m in report.metrics}
    assert by["run_to_run_agreement"].value == 0.5 and by["scorer_level_accuracy"].value == 0.75


def test_the_scorer_eval_stops_at_the_spend_cap():
    report = run_scorer_eval(
        make_scorer(perfect()), SCORER_CASES, CONFIG.targets, max_cost_usd=0.0001
    )
    assert report.cases < len(SCORER_CASES) and "spend cap" in report.notes


# --- verifier eval ------------------------------------------------------------------------


def verifier_with(*verdicts):
    provider = TailorProvider(verifier=list(verdicts))
    _, verifier = agents(provider, load_tailoring_config())
    return verifier, provider


def ideal_verdicts():
    """What a good Verifier says: reject the 3 drafts aimed at it, accept the 2 clean ones."""
    order = [c for c in VERIFIER_CASES if c.expected_layer != "code"]
    return [verdict("traced") if c.kind == "clean" else verdict("inflated") for c in order]


def test_a_good_verifier_stops_every_fabrication_and_never_sees_the_code_caught_one():
    verifier, provider = verifier_with(*ideal_verdicts())
    report = run_verifier_eval(verifier, MASTER, VERIFIER_CASES, CONFIG.targets)
    by = {m.name: m.value for m in report.metrics}
    assert (
        by["verifier_escape_rate"] == 0 and by["verifier_false_alarm_rate"] == 0 and report.passed
    )
    assert (
        by["caught_by_code"] == 0.25
        and by["caught_by_verifier"] == 0.75
        and by["right_layer_caught_it"] == 1.0
    )
    assert len(provider.calls) == 5  # 6 cases, but the invented number never reached the model


def test_a_lenient_verifier_lets_fabrications_escape_and_the_eval_fails():
    verifier, _ = verifier_with(*[verdict("traced")] * 5)
    report = run_verifier_eval(verifier, MASTER, VERIFIER_CASES, CONFIG.targets)
    by = {m.name: m for m in report.metrics}
    assert by["verifier_escape_rate"].value == 0.75 and by["verifier_escape_rate"].passed is False
    assert not report.passed and sum("ESCAPED" in f for f in report.failures) == 3
    assert any("corrupt-2-inflated-verb" in f for f in report.failures)


def test_a_paranoid_verifier_rejecting_clean_drafts_is_a_false_alarm():
    verifier, _ = verifier_with(*[verdict("inflated")] * 5)
    report = run_verifier_eval(verifier, MASTER, VERIFIER_CASES, CONFIG.targets)
    by = {m.name: m for m in report.metrics}
    assert (
        by["verifier_false_alarm_rate"].value == 1.0
        and by["verifier_false_alarm_rate"].passed is False
    )
    assert by["verifier_escape_rate"].value == 0  # nothing escaped, it just cried wolf
    assert any("clean draft rejected" in f for f in report.failures)


# --- reporting ------------------------------------------------------------------------------


def test_metric_pass_logic_handles_both_directions_and_informational_metrics():
    assert (
        Metric("a", 0.95, 0.90, ">=").passed is True
        and Metric("a", 0.85, 0.90, ">=").passed is False
    )
    assert (
        Metric("b", 0.0, 0.0, "<=").passed is True and Metric("b", 0.1, 0.0, "<=").passed is False
    )
    assert Metric("c", 0.5).passed is None


def test_results_are_logged_one_row_per_metric_to_the_evals_tab(tmp_path):
    report = EvalReport(
        "scorer",
        "claude-haiku-4-5",
        2,
        9,
        [Metric("acc", 0.8889, 0.9), Metric("info", 1.0)],
        0.0712,
        notes="x",
    )
    storage = CsvStorage(str(tmp_path))
    log_report(storage, report, datetime(2026, 10, 9, 12, 0, tzinfo=UTC))
    rows = storage.read_records("Evals")
    assert [r["metric"] for r in rows] == ["acc", "info"] and set(rows[0]) == set(EVALS_HEADERS)
    assert (
        rows[0]["passed"] == "false"
        and rows[0]["target"] == ">=0.90"
        and rows[1]["passed"] == ""
        and rows[1]["target"] == ""
    )
    assert (
        rows[0]["prompt_version"] == "2"
        and rows[0]["eval_id"] == "20261009T120000Z-scorer"
        and rows[0]["cost_usd"] == "0.0712"
    )


def test_the_report_renders_for_a_person():
    report = run_scorer_eval(make_scorer(perfect()), SCORER_CASES, CONFIG.targets)
    console = Console(file=StringIO(), width=120, record=True)
    report.render(console)
    text = console.export_text()
    assert "scorer_level_accuracy" in text and "pass" in text and "Cost:" in text


def test_the_yaml_data_files_use_only_known_keys():
    for path in (CONFIG.scorer_cases, CONFIG.verifier_cases):
        for case in yaml.safe_load(Path(path).read_text()):
            assert "id" in case


def test_accuracy_is_reported_separately_for_dev_and_held_out_cases():
    levels = perfect()
    first_held_out = next(i for i, c in enumerate(SCORER_CASES) if c.split == "held_out")
    levels[first_held_out] = "vp"  # break exactly one held-out case
    by = {
        m.name: m
        for m in run_scorer_eval(make_scorer(levels), SCORER_CASES, CONFIG.targets).metrics
    }
    assert by["accuracy_dev"].value == 1.0 and by["accuracy_held_out"].value < 1.0
    assert by["accuracy_held_out"].passed is None  # informational: the target applies to the total


def test_the_report_fingerprints_the_full_instructions_so_profile_changes_are_visible():
    a = run_scorer_eval(make_scorer(perfect()), SCORER_CASES, CONFIG.targets)
    other = Scorer(
        ScorerProvider([output() for _ in SCORER_CASES]),
        load_scoring_config(),
        Prompt(7, "P"),
        "resume",
        "DIFFERENT PROFILES",
    )
    b = run_scorer_eval(other, SCORER_CASES, CONFIG.targets)
    fa = next(x for x in a.notes.split("; ") if x.startswith("instructions "))
    fb = next(x for x in b.notes.split("; ") if x.startswith("instructions "))
    assert fa != fb and len(fa.split()[1]) == 8


# --- the quickstart's example resume must actually work with the code ---------------------------


def test_the_example_master_resume_is_a_valid_starting_point_for_a_new_user():
    from jobagent.agents.context import render_resume
    from jobagent.orchestrator.checks import MasterIndex

    example = load_yaml("config/master_resume.example.yaml")
    master = MasterIndex.from_resume(example)  # every role and bullet id resolves, ids are unique
    ids = [b["id"] for c in example["experience"] for r in c["roles"] for b in r["bullets"]]
    assert len(ids) == len(set(ids)) >= 5 and set(ids) <= set(master.bullets)
    text = render_resume(example, include_ids=True)
    assert (
        f"[{ids[0]}]" in text and "avery@example.com" not in text
    )  # contact details never reach the model
    assert "example" in Path("config/master_resume.example.yaml").read_text().lower().split("\n")[0]


def test_the_license_and_package_metadata_agree():
    root = Path(__file__).parents[1]
    assert (root / "LICENSE").read_text().startswith("MIT License")
    assert 'license = "MIT"' in (root / "pyproject.toml").read_text()
