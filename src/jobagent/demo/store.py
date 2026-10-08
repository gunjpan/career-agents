import shutil
from pathlib import Path


class LocalArtifactStore:
    """The ArtifactStore interface on a local folder instead of Google Drive (used by the demo)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(name: str) -> str:
        return "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip()

    def create_job_folder(self, name: str) -> tuple[str, str]:
        return self.create_subfolder(name, str(self.root))

    def create_subfolder(self, name: str, parent_id: str) -> tuple[str, str]:
        folder = Path(parent_id) / self._safe(name)
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder), f"file://{folder}"

    def upload_bytes(
        self, name: str, data: bytes, mime_type: str, parent_id: str | None = None
    ) -> str:
        path = Path(parent_id or self.root) / self._safe(name)
        path.write_bytes(data)
        return str(path)

    def download_bytes(self, file_id: str) -> bytes:
        return Path(file_id).read_bytes()

    def trash(self, file_id: str) -> None:
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        shutil.move(file_id, trash / Path(file_id).name)
