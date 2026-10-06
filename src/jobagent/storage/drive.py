from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from jobagent.settings import Settings

# drive.file: the app sees only files it created itself, so it owns its root folder.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"


def run_consent_flow(settings: Settings) -> Credentials:
    """One-time browser consent as the user; saves the refresh token to the token file."""
    flow = InstalledAppFlow.from_client_secrets_file(
        settings.google_oauth_client_file, DRIVE_SCOPES
    )
    creds = flow.run_local_server(port=0)
    with open(settings.google_oauth_token_file, "w") as f:
        f.write(creds.to_json())
    return creds


def drive_credentials(settings: Settings) -> Credentials:
    creds = Credentials.from_authorized_user_info(settings.oauth_token_info(), DRIVE_SCOPES)
    if not creds.valid:
        creds.refresh(Request())  # exchanges the refresh token for a fresh access token
    return creds


class DriveStorage:
    def __init__(self, settings: Settings, credentials: Credentials | None = None) -> None:
        self.folder_id = settings.drive_folder_id
        creds = credentials or drive_credentials(settings)
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        meta: dict = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            meta["parents"] = [parent_id]
        return self.service.files().create(body=meta, fields="id").execute()["id"]

    def upload_bytes(self, name: str, data: bytes, mime_type: str) -> str:
        """Upload a file into the configured folder; returns the Drive file id."""
        media = MediaInMemoryUpload(data, mimetype=mime_type)
        meta = {"name": name, "parents": [self.folder_id]}
        return self.service.files().create(body=meta, media_body=media, fields="id").execute()["id"]

    def delete(self, file_id: str) -> None:
        self.service.files().delete(fileId=file_id).execute()
