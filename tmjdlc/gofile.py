"""Persistent GoFile account uploader with 20-video public folder rotation."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

API_BASE = "https://api.gofile.io"
UPLOAD_URL = os.getenv("GOFILE_UPLOAD_URL", "https://upload.gofile.io/uploadfile").strip()
TIMEOUT = (30, 3600)
VIDEOS_PER_FOLDER = 20
_upload_lock = threading.RLock()


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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_public_folder(queue, token: str, root_folder: str, number: int) -> dict[str, str]:
    response = requests.post(
        f"{API_BASE}/contents/createFolder",
        headers=_auth(token),
        json={
            "parentFolderId": root_folder,
            "folderName": f"TMJDLC Batch {number:03d}",
            "public": True,
        },
        timeout=30,
    )
    folder = _json_data(response)
    folder_id = str(folder.get("id") or "").strip()
    folder_code = str(folder.get("code") or "").strip()
    if not folder_id or not folder_code:
        raise GoFileError("GoFile did not return a public folder ID and share code.")
    folder_url = f"https://gofile.io/d/{folder_code}"
    queue.set_setting("gofile_folder_id", folder_id)
    queue.set_setting("gofile_folder_code", folder_code)
    queue.set_setting("gofile_folder_url", folder_url)
    queue.set_setting("gofile_folder_count", "0")
    queue.set_setting("gofile_folder_number", str(number))
    return {
        "folder_id": folder_id,
        "folder_code": folder_code,
        "folder_url": folder_url,
        "folder_number": str(number),
        "folder_count": "0",
    }


def _ensure_account(queue) -> tuple[str, str]:
    token = str(os.getenv("GOFILE_API_TOKEN", "") or queue.get_setting("gofile_guest_token") or "").strip()
    root_folder = str(queue.get_setting("gofile_root_folder") or "").strip()

    configured_token = str(os.getenv("GOFILE_API_TOKEN", "") or "").strip()
    if configured_token and configured_token != str(queue.get_setting("gofile_guest_token") or ""):
        # A supplied account API token takes precedence over an older guest token.
        account = _json_data(requests.get(
            f"{API_BASE}/accounts/getid", headers=_auth(configured_token), timeout=30
        ))
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            raise GoFileError("GoFile did not return an account ID for GOFILE_API_TOKEN.")
        details = _json_data(requests.get(
            f"{API_BASE}/accounts/{account_id}", headers=_auth(configured_token), timeout=30
        ))
        root_folder = str(details.get("rootFolder") or "").strip()
        if not root_folder:
            raise GoFileError("GoFile account details did not include rootFolder.")
        token = configured_token
        queue.set_setting("gofile_guest_token", token)
        queue.set_setting("gofile_root_folder", root_folder)
        queue.set_setting("gofile_account_id", account_id)
    elif not token:
        account = _json_data(requests.post(f"{API_BASE}/accounts", json={}, timeout=30))
        token = str(account.get("token") or account.get("guestToken") or "").strip()
        root_folder = str(account.get("rootFolder") or "").strip()
        if not token or not root_folder:
            raise GoFileError("GoFile did not return a guest token and root folder.")
        queue.set_setting("gofile_guest_token", token)
        queue.set_setting("gofile_root_folder", root_folder)

    if not root_folder:
        # Existing stored tokens may predate root-folder persistence.
        account = _json_data(requests.get(
            f"{API_BASE}/accounts/getid", headers=_auth(token), timeout=30
        ))
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            raise GoFileError("GoFile did not return an account ID.")
        details = _json_data(requests.get(
            f"{API_BASE}/accounts/{account_id}", headers=_auth(token), timeout=30
        ))
        root_folder = str(details.get("rootFolder") or "").strip()
        if not root_folder:
            raise GoFileError("GoFile account details did not include rootFolder.")
        queue.set_setting("gofile_root_folder", root_folder)
    return token, root_folder


def ensure_guest_folder(queue) -> dict[str, str]:
    """Ensure a reusable GoFile account and the current public 20-video folder exist."""
    with _upload_lock:
        token, root_folder = _ensure_account(queue)
        folder_id = str(queue.get_setting("gofile_folder_id") or "").strip()
        folder_count = int(queue.get_setting("gofile_folder_count", "0") or 0)
        folder_number = int(queue.get_setting("gofile_folder_number", "1") or 1)

        if not folder_id or folder_count >= VIDEOS_PER_FOLDER:
            folder_number = max(1, folder_number + (1 if folder_id else 0))
            return _create_public_folder(queue, token, root_folder, folder_number)

        folder_code = str(queue.get_setting("gofile_folder_code") or "").strip()
        folder_url = str(queue.get_setting("gofile_folder_url") or "").strip()
        if not folder_code or not folder_url:
            # Recover the share code from a folder newly created by this integration.
            raise GoFileError("The current GoFile folder is missing its public share link.")
        return {
            "folder_id": folder_id,
            "folder_code": folder_code,
            "folder_url": folder_url,
            "folder_number": str(folder_number),
            "folder_count": str(folder_count),
        }


def upload_file(queue, path: str | Path) -> dict[str, Any]:
    """Upload one video, rotate after 20 successful videos, and return completion metadata."""
    file_path = Path(path)
    if not file_path.is_file() or file_path.stat().st_size <= 0:
        raise GoFileError(f"Upload file is missing or empty: {file_path.name}")

    with _upload_lock:
        destination = ensure_guest_folder(queue)
        token = str(queue.get_setting("gofile_guest_token") or "").strip()
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
                    **_auth(token),
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

        new_count = int(destination["folder_count"]) + 1
        queue.set_setting("gofile_folder_count", str(new_count))
        completed_folder = None
        if new_count >= VIDEOS_PER_FOLDER:
            completed_folder = {
                "folder_id": destination["folder_id"],
                "folder_code": destination["folder_code"],
                "folder_url": destination["folder_url"],
                "folder_number": destination["folder_number"],
                "video_count": new_count,
            }
            # Create the next public folder immediately. If this fails, the count
            # remains at 20 and the next upload retries folder creation safely.
            token, root_folder = _ensure_account(queue)
            _create_public_folder(
                queue, token, root_folder, int(destination["folder_number"]) + 1
            )

        return {
            "file_id": str(data.get("id") or ""),
            "file_name": str(data.get("name") or file_path.name),
            "file_size": int(data.get("size") or file_path.stat().st_size),
            "download_url": download_url,
            "folder_id": destination["folder_id"],
            "folder_url": destination["folder_url"],
            "folder_number": destination["folder_number"],
            "folder_count": new_count,
            "folder_completed": completed_folder,
        }


def finalize_current_folder(queue) -> dict[str, Any] | None:
    """Close and return a non-empty partial folder when the upload queue becomes idle."""
    with _upload_lock:
        count = int(queue.get_setting("gofile_folder_count", "0") or 0)
        folder_id = str(queue.get_setting("gofile_folder_id") or "").strip()
        if not folder_id or count <= 0 or count >= VIDEOS_PER_FOLDER:
            return None
        folder = {
            "folder_id": folder_id,
            "folder_code": str(queue.get_setting("gofile_folder_code") or ""),
            "folder_url": str(queue.get_setting("gofile_folder_url") or ""),
            "folder_number": str(queue.get_setting("gofile_folder_number", "1") or "1"),
            "video_count": count,
        }
        token, root_folder = _ensure_account(queue)
        _create_public_folder(queue, token, root_folder, int(folder["folder_number"]) + 1)
        return folder
