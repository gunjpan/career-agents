from typing import Protocol


class StorageError(Exception):
    pass


class Storage(Protocol):
    """Storage interface: Sheets now, Postgres later. Callers depend only on this."""

    def append_row(self, tab: str, row: list[str]) -> None: ...

    def read_rows(self, tab: str) -> list[list[str]]: ...

    def ensure_headers(self, tab: str, headers: list[str]) -> None:
        """Write the header row if the tab is empty; raise StorageError if it differs."""
        ...

    def read_records(self, tab: str) -> list[dict[str, str]]:
        """Rows as dicts keyed by the header row. Blank rows are skipped."""
        ...

    def append_records(self, tab: str, headers: list[str], records: list[dict[str, str]]) -> None: ...


def check_headers(tab: str, existing: list[str], expected: list[str]) -> None:
    if existing != expected:
        raise StorageError(f"tab {tab!r} has headers {existing}, expected {expected}")


def rows_to_records(rows: list[list[str]]) -> list[dict[str, str]]:
    non_blank = [r for r in rows if any(c.strip() for c in r)]
    if not non_blank:
        return []
    header, *body = non_blank
    return [dict(zip(header, row + [""] * (len(header) - len(row)), strict=False)) for row in body]
