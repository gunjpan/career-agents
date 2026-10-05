import gspread
from google.oauth2.service_account import Credentials

from jobagent.settings import Settings

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
