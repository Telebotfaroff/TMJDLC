from __future__ import annotations

from pathlib import Path
from typing import Any


class DriveManager:
    """Google Drive v3 operations; authentication is supplied by the runtime."""

    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, service: Any, root_name: str = "TMJDLC Library", root_id: str | None = None):
        self.service = service
        self.root_name = root_name
        self.root_id = root_id

    @classmethod
    def from_colab(cls, root_name: str = "TMJDLC Library", root_id: str | None = None):
        try:
            from google.colab import auth
            from google.auth import default
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError("Run this inside Google Colab and install the Google API requirements") from exc
        auth.authenticate_user()
        credentials, _ = default()
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return cls(service, root_name, root_id)

    def ensure_root(self) -> str:
        if self.root_id:
            metadata = self.service.files().get(fileId=self.root_id, fields="id,mimeType,trashed").execute()
            if metadata.get("trashed") or metadata.get("mimeType") != self.FOLDER_MIME:
                raise ValueError("Configured Drive root ID is not an active folder")
            return self.root_id
        escaped = self.root_name.replace("'", "\\'")
        response = self.service.files().list(
            q=f"name='{escaped}' and mimeType='{self.FOLDER_MIME}' and trashed=false",
            spaces="drive", pageSize=10, fields="files(id,name)"
        ).execute()
        matches = response.get("files", [])
        if matches:
            self.root_id = matches[0]["id"]
        else:
            created = self.service.files().create(
                body={"name": self.root_name, "mimeType": self.FOLDER_MIME},
                fields="id"
            ).execute()
            self.root_id = created["id"]
        return self.root_id

    def ensure_code_folder(self, code: str) -> dict[str, str]:
        key = code.strip().upper()
        if not key or any(char in key for char in "/\\"):
            raise ValueError("Invalid video code")
        root = self.ensure_root()
        escaped = key.replace("'", "\\'")
        response = self.service.files().list(
            q=f"name='{escaped}' and '{root}' in parents and mimeType='{self.FOLDER_MIME}' and trashed=false",
            spaces="drive", pageSize=10, fields="files(id,name,webViewLink)"
        ).execute()
        matches = response.get("files", [])
        if matches:
            folder = matches[0]
        else:
            folder = self.service.files().create(
                body={"name": key, "mimeType": self.FOLDER_MIME, "parents": [root]},
                fields="id,name,webViewLink"
            ).execute()
        folder_id = folder["id"]
        url = folder.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder_id}"
        return {"id": folder_id, "url": url, "name": key}

    def upload_file(
        self, local_path: str | Path, folder_id: str, *,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Upload resumably and report (uploaded_bytes, total_bytes) after each chunk."""
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise RuntimeError("google-api-python-client is not installed") from exc
        path = Path(local_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        total = path.stat().st_size
        media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True)
        request = self.service.files().create(
            body={"name": path.name, "parents": [folder_id]},
            media_body=media,
            fields="id,name,size,webViewLink"
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status is not None and progress_callback:
                progress_callback(int(status.progress() * total), total)
        if not response.get("id") or int(response.get("size", -1)) != total:
            raise IOError("Drive upload verification failed: ID or size mismatch")
        if progress_callback:
            progress_callback(total, total)
        return response

    def write_index(self, entries: dict[str, str], filename: str = "index.txt") -> str:
        """Create/update root index. Entries are code -> folder URL."""
        from googleapiclient.http import MediaInMemoryUpload
        root = self.ensure_root()
        content = "".join(f"{code.upper()} {{{url}}}\n" for code, url in sorted(entries.items()))
        response = self.service.files().list(
            q=f"name='{filename}' and '{root}' in parents and trashed=false",
            spaces="drive", pageSize=10, fields="files(id)"
        ).execute()
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain", resumable=False)
        files = response.get("files", [])
        if files:
            file_id = files[0]["id"]
            self.service.files().update(fileId=file_id, media_body=media).execute()
            return file_id
        created = self.service.files().create(
            body={"name": filename, "parents": [root], "mimeType": "text/plain"},
            media_body=media, fields="id"
        ).execute()
        return created["id"]
