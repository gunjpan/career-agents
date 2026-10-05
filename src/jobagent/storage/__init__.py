from jobagent.settings import Settings
from jobagent.storage.base import Storage


def get_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "csv":
        from jobagent.storage.csv_store import CsvStorage

        return CsvStorage("data")
    from jobagent.storage.sheets import SheetsStorage

    return SheetsStorage(settings)
