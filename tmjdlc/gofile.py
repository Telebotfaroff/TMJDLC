"""Persistent GoFile guest-account uploader for TMJDLC."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

API_BASE = "https://api.gofile.io"
UPLOAD_URL = "https://upload.gofile.io/uploadfile"
TIMEOUT = (30, 3600)


class GoFileError(RuntimeError):
    """GoFile API or upload operation failed."""


def _json_data(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GoFileError(
            f"GoFile returned a non-JSON response (HTTP {response.status_code})."
        ) from exc

    if not response.ok or payload.get("status") != "ok":
        status = payload.get("status") or f"HTTP {response.status_code}"
        raise GoFileError(f"GoFile request failed: {status}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GoFileError("GoFile returned an unexpected response payload.")
    return data


def ensure_guest_folder(queue) -> dict[str, str]:
    """Create one guest account and one public folder, then persist their credentials."""
    token = queue.get_setting("gofile_guest_token")
    root_folder = queue.get_setting("gofile_root_folder")
    folder_id = queue.get_setting("gofile_folder_id")
    folder_code = queue.get_setting("gofile_folder_code")

    if not token:
        response = requests.post(f"{API_BASE}/accounts", json={}, timeout=30)
        account = _json_data(response)
        token = str(account.get("token") or account.get("guestToken") or "").strip()
        root_folder = str(account.get("rootFolder") or "").strip()
        if not token or not root_folder:
            raise GoFileError("GoFile did not return a guest token and root folder.")
        # Persist the token immediately so a restart during folder creation
        # resumes this same guest account instead of creating another one.
        queue.set_setting("gofile_guest_token", token)
        queue.set_setting("gofile_root_folder", root_folder)

    if not folder_id:
        response = requests.post(
            f"{API_BASE}/contents/createFolder",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "parentFolderId": root_folder,
                "folderName": "TMJDLC Uploads",
                "public": True,
            },
            timeout=30,
        )
        folder = _json_data(response)
        folder_id = str(folder.get("id") or "").strip()
        folder_code = str(folder.get("code") or "").strip()
        if not folder_id:
            raise GoFileError("GoFile did not return the shared folder ID.")
        queue.set_setting("gofile_folder_id", folder_id)
        if folder_code:
            queue.set_setting("gofile_folder_code", folder_code)

    folder_url = f"https://gofile.io/d/{folder_code}" if folder_code else ""
    if folder_url:
        queue.set_setting("gofile_folder_url", folder_url)
    return {
        "folder_id": folder_id,
        "folder_code": folder_code or "",
        "folder_url": folder_url,
    }


def upload_file(queue, path: str | Path) -> dict[str, Any]:
    """Stream one file into the persistent guest folder and return its share links."""
    file_path = Path(path)
    if not file_path.is_file() or file_path.stat().st_size <= 0:
        raise GoFileError(f"Upload file is missing or empty: {file_path.name}")

    destination = ensure_guest_folder(queue)
    token = queue.get_setting("gofile_guest_token")
    file_handle = file_path.open("rb")
    try:
        encoder = MultipartEncoder(fields={
            "file": (file_path.name, file_handle, "application/octet-stream"),
            "folderId": destination["folder_id"],
        })
        response = requests.post(
            UPLOAD_URL,
            data=encoder,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": encoder.content_type,
            },
            timeout=TIMEOUT,
        )
        data = _json_data(response)
    finally:
        file_handle.close()

    download_url = str(data.get("downloadPage") or "").strip()
    file_code = str(data.get("code") or "").strip()
    if not download_url and file_code:
        download_url = f"https://gofile.io/d/{file_code}"
    if not download_url:
        raise GoFileError("GoFile accepted the upload but returned no share link.")

    return {
        "file_id": str(data.get("id") or ""),
        "file_name": str(data.get("name") or file_path.name),
        "file_size": int(data.get("size") or file_path.stat().st_size),
        "download_url": download_url,
        "folder_id": destination["folder_id"],
        "folder_url": destination["folder_url"],
    }
