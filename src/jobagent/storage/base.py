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

    def append_records(
        self, tab: str, headers: list[str], records: list[dict[str, str]]
    ) -> None: ...

    def update_records(self, tab: str, key: str, updates: dict[str, dict[str, str]]) -> int:
        """For each {key value: {column: new value}}, set those cells on the row whose `key`
        column matches. Other columns are untouched. Returns how many rows were found."""
        ...


def reconcile_headers(tab: str, existing: list[str], expected: list[str]) -> bool:
    """True if columns were added at the end and the header row needs extending.

    Additive changes (new columns on the right) migrate automatically; anything else,
    such as renamed, reordered or removed columns, raises instead of corrupting the tab.
    """
    existing = list(existing)
    while existing and not existing[-1]:
        existing.pop()  # Sheets pads rows with empty cells
    if existing == expected:
        return False
    if len(existing) < len(expected) and existing == expected[: len(existing)]:
        return True
    raise StorageError(f"tab {tab!r} has headers {existing}, expected {expected}")


def rows_to_records(rows: list[list[str]]) -> list[dict[str, str]]:
    non_blank = [r for r in rows if any(c.strip() for c in r)]
    if not non_blank:
        return []
    header, *body = non_blank
    return [dict(zip(header, row + [""] * (len(header) - len(row)), strict=False)) for row in body]
