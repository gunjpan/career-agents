import csv
from pathlib import Path


class CsvStorage:
    """Local fallback: one CSV file per tab."""

    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, tab: str) -> Path:
        return self.dir / f"{tab}.csv"

    def append_row(self, tab: str, row: list[str]) -> None:
        with self._path(tab).open("a", newline="") as f:
            csv.writer(f).writerow(row)

    def read_rows(self, tab: str) -> list[list[str]]:
        if not self._path(tab).exists():
            return []
        with self._path(tab).open(newline="") as f:
            return list(csv.reader(f))
