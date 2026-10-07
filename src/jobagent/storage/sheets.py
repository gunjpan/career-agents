import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from jobagent.settings import Settings
from jobagent.storage.base import reconcile_headers, rows_to_records

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
        if not rows or reconcile_headers(tab, rows[0], headers):
            ws = self.book.worksheet(tab)
            if len(headers) > ws.col_count:  # new columns can outgrow the sheet's grid
                ws.add_cols(len(headers) - ws.col_count)
            ws.update([headers], "A1", raw=True)

    def read_records(self, tab: str) -> list[dict[str, str]]:
        return rows_to_records(self.read_rows(tab))

    def append_records(self, tab: str, headers: list[str], records: list[dict[str, str]]) -> None:
        self.ensure_headers(tab, headers)
        if records:
            rows = [[rec.get(h, "") for h in headers] for rec in records]
            self.book.worksheet(tab).append_rows(rows, value_input_option="RAW")

    def update_records(self, tab: str, key: str, updates: dict[str, dict[str, str]]) -> int:
        ws = self.book.worksheet(tab)
        rows = ws.get_all_values()
        if not rows:
            return 0
        header = rows[0]
        key_col = header.index(key)
        data, found = [], 0
        for number, row in enumerate(rows[1:], start=2):  # 1-based sheet rows; row 1 is the header
            cell = row[key_col] if key_col < len(row) else ""
            if cell not in updates:
                continue
            found += 1
            for col, value in updates[cell].items():
                data.append(
                    {"range": rowcol_to_a1(number, header.index(col) + 1), "values": [[value]]}
                )
        if data:
            ws.batch_update(data, value_input_option="RAW")  # one API request for all cells
        return found
