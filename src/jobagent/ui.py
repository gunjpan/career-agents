"""Terminal progress widgets: a scramble-to-reveal spinner and a clock that never freezes."""

import os
import random
from datetime import timedelta

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TextColumn,
)
from rich.text import Text

PHRASES = ("gunjpan", "career-agents", "judge, then verify", "code for control")
WIDTH = max(len(p) for p in PHRASES)
GLYPHS = "abcdefghijklmnopqrstuvwxyz0123456789#%&*+=<>/"
REVEAL_SECONDS = 0.9  # time to lock all letters, left to right
HOLD_SECONDS = 1.6  # time the finished phrase stays readable
FRAMES_PER_SECOND = 14


def animation_enabled(console: Console) -> bool:
    """Animate only in a real terminal. CI logs, pipes and JOBAGENT_NO_ANIM=1 get the plain spinner."""
    return console.is_terminal and not os.environ.get("JOBAGENT_NO_ANIM")


def scramble_frame(t: float, seed: int = 0) -> str:
    """The text to show at time t. Deterministic for a given (t, seed), so it is testable."""
    cycle = REVEAL_SECONDS + HOLD_SECONDS
    phrase = PHRASES[int(t // cycle) % len(PHRASES)]
    into = t % cycle
    locked = len(phrase) if into >= REVEAL_SECONDS else int(len(phrase) * into / REVEAL_SECONDS)
    rng = random.Random(f"{seed}:{int(t * FRAMES_PER_SECOND)}")
    shown = [c if i < locked or c == " " else rng.choice(GLYPHS) for i, c in enumerate(phrase)]
    return "".join(shown).ljust(WIDTH)


class ScrambleColumn(ProgressColumn):
    """Cycles through PHRASES, each scrambling into place. Redraws at FRAMES_PER_SECOND."""

    max_refresh = 1 / FRAMES_PER_SECOND

    def render(self, task: Task) -> Text:
        text = scramble_frame(task.get_time())
        locked = text.strip() in PHRASES
        return Text(text, style="bold cyan" if locked else "dim cyan")


class WallClockColumn(ProgressColumn):
    """Elapsed time since the first render.

    rich's TimeElapsedColumn stops the moment a task's completed count reaches its total. The
    pipeline reports done == total at the end of each company, so from the second company on
    the built-in clock stayed frozen while the next fetch ran."""

    def __init__(self) -> None:
        super().__init__()
        self._t0: float | None = None

    def render(self, task: Task) -> Text:
        now = task.get_time()
        if self._t0 is None:
            self._t0 = now
        return Text(str(timedelta(seconds=int(now - self._t0))), style="progress.elapsed")


def make_progress(console: Console) -> Progress:
    spinner = ScrambleColumn() if animation_enabled(console) else SpinnerColumn()
    return Progress(
        spinner,
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        WallClockColumn(),
        console=console,
        refresh_per_second=FRAMES_PER_SECOND,
    )
