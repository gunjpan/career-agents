import json
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed config read from env vars / .env. Secrets never live in code."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    google_service_account_file: str = "secrets/service-account.json"
    google_service_account_json: SecretStr | None = None
    # Drive uses OAuth as the user: service accounts have no Drive storage quota.
    google_oauth_client_file: str = "secrets/oauth-client.json"
    google_oauth_token_file: str = "secrets/drive-token.json"
    google_oauth_token_json: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None  # also read from the real env var in CI
    sheet_id: str = ""
    drive_folder_id: str = ""
    storage_backend: Literal["sheets", "csv"] = "sheets"

    def service_account_info(self) -> dict:
        """Raw JSON env var (CI) wins over the key file (local)."""
        if self.google_service_account_json:
            return json.loads(self.google_service_account_json.get_secret_value())
        with open(self.google_service_account_file) as f:
            return json.load(f)

    def oauth_token_info(self) -> dict:
        """Authorized-user JSON (refresh token + client id/secret). Env var wins, as above."""
        if self.google_oauth_token_json:
            return json.loads(self.google_oauth_token_json.get_secret_value())
        with open(self.google_oauth_token_file) as f:
            return json.load(f)


def get_settings() -> Settings:
    return Settings()
