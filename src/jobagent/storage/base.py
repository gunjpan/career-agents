from typing import Protocol


class Storage(Protocol):
    """Storage interface: Sheets now, Postgres later. Callers depend only on this."""

    def append_row(self, tab: str, row: list[str]) -> None: ...

    def read_rows(self, tab: str) -> list[list[str]]: ...
