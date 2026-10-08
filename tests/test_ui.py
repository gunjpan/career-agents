from io import StringIO

from rich.console import Console
from rich.progress import Progress, SpinnerColumn

from jobagent.ui import (
    PHRASES,
    WIDTH,
    ScrambleColumn,
    WallClockColumn,
    animation_enabled,
    make_progress,
    scramble_frame,
)


def test_frames_are_deterministic_and_fixed_width():
    assert scramble_frame(0.3, seed=1) == scramble_frame(0.3, seed=1)
    assert all(len(scramble_frame(t / 10)) == WIDTH for t in range(100))


def test_each_phrase_ends_fully_revealed():
    cycle_time = 0.9 + 1.6
    for i, phrase in enumerate(PHRASES):
        assert scramble_frame(i * cycle_time + 1.0).strip() == phrase


def test_clock_keeps_running_after_task_reaches_its_total():
    now = [0.0]
    col = WallClockColumn()
    progress = Progress(
        col, console=Console(file=StringIO()), get_time=lambda: now[0], auto_refresh=False
    )
    task = progress.add_task("x", total=None)
    col.render(progress.tasks[0])  # first draw starts the clock at t=0
    now[0] = 10
    progress.update(task, completed=5, total=5)  # finished, as at the end of a company
    now[0] = 95
    progress.update(task, completed=0)  # next company starts
    assert col.render(progress.tasks[0]).plain == "0:01:35"


def test_plain_spinner_when_not_a_terminal():
    console = Console(file=StringIO(), force_terminal=False)
    assert not animation_enabled(console)
    assert isinstance(make_progress(console).columns[0], SpinnerColumn)


def test_animation_in_terminal_and_opt_out(monkeypatch):
    console = Console(file=StringIO(), force_terminal=True)
    monkeypatch.delenv("JOBAGENT_NO_ANIM", raising=False)
    assert isinstance(make_progress(console).columns[0], ScrambleColumn)
    monkeypatch.setenv("JOBAGENT_NO_ANIM", "1")
    assert isinstance(make_progress(console).columns[0], SpinnerColumn)
