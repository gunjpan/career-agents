import gspread
from google.oauth2.service_account import Credentials

from jobagent.settings import Settings
from jobagent.storage.base import check_headers, rows_to_records

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def google_credentials(settings: Settings) -> Credentials:
    return Credentials.from_service_account_info(settings.service_account_info(), scopes=SCOPES)


class SheetsStorage:
    def __init__(self, settings: Settings) -> None:
        client = gspread.authorize(google_credentials(settings))
        self.book = client.open_by_key(settings.sheet_id)

    def append_row(self, tab: str, row: list[str]) -> None:
        self.book.worksheet(tab).append_row(row, value_input_option="RAW")

    def read_rows(self, tab: str) -> list[list[str]]:
        return self.book.worksheet(tab).get_all_values()

    def ensure_headers(self, tab: str, headers: list[str]) -> None:
        rows = [r for r in self.read_rows(tab) if any(r)]
        if not rows:
            self.book.worksheet(tab).update([headers], "A1", raw=True)
        else:
            check_headers(tab, rows[0][: len(headers)], headers)

    def read_records(self, tab: str) -> list[dict[str, str]]:
        return rows_to_records(self.read_rows(tab))

    def append_records(self, tab: str, headers: list[str], records: list[dict[str, str]]) -> None:
        self.ensure_headers(tab, headers)
        if records:
            rows = [[rec.get(h, "") for h in headers] for rec in records]
            self.book.worksheet(tab).append_rows(rows, value_input_option="RAW")
