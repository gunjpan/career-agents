import csv
from pathlib import Path

from jobagent.storage.base import reconcile_headers, rows_to_records


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

    def ensure_headers(self, tab: str, headers: list[str]) -> None:
        rows = [r for r in self.read_rows(tab) if any(r)]
        if not rows:
            self.append_row(tab, headers)
        elif reconcile_headers(tab, rows[0], headers):
            with self._path(tab).open("w", newline="") as f:
                csv.writer(f).writerows([headers, *rows[1:]])

    def read_records(self, tab: str) -> list[dict[str, str]]:
        return rows_to_records(self.read_rows(tab))

    def append_records(self, tab: str, headers: list[str], records: list[dict[str, str]]) -> None:
        self.ensure_headers(tab, headers)
        for rec in records:
            self.append_row(tab, [rec.get(h, "") for h in headers])

    def update_records(self, tab: str, key: str, updates: dict[str, dict[str, str]]) -> int:
        rows = self.read_rows(tab)
        if not rows:
            return 0
        header = rows[0]
        key_col, found = header.index(key), 0
        for row in rows[1:]:
            row += [""] * (len(header) - len(row))
            if row[key_col] in updates:
                found += 1
                for col, value in updates[row[key_col]].items():
                    row[header.index(col)] = value
        with self._path(tab).open("w", newline="") as f:
            csv.writer(f).writerows(rows)
        return found
