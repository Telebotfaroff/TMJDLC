from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

import requests


API_BASE = "https://api.gofile.io"
UPLOAD_URL = "https://upload.gofile.io/uploadfile"
TIMEOUT = (20, 60)


class GoFileError(RuntimeError):
    pass


class GoFileClient:
    def __init__(self, state: dict[str, Any], save_state, api_token: str = "") -> None:
        self.state = state
        self.save_state = save_state
        self.api_token = api_token.strip()

    @property
    def token(self) -> str:
        return str(self.state.get("gofile_token") or self.api_token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @staticmethod
    def _data(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoFileError(f"GoFile returned non-JSON data (HTTP {response.status_code})") from exc
        if response.status_code >= 400 or payload.get("status") != "ok":
            raise GoFileError(
                f"GoFile API error: {payload.get('status', response.status_code)}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GoFileError("GoFile response did not contain a data object")
        return data

    def ensure_account(self) -> None:
        if self.token and self.state.get("root_folder_id"):
            return
        if not self.token:
            response = requests.post(f"{API_BASE}/accounts", json={}, timeout=TIMEOUT)
            data = self._data(response)
            self.state["gofile_token"] = str(data.get("token") or "")
            self.state["root_folder_id"] = str(data.get("rootFolder") or "")
            if not self.state["gofile_token"] or not self.state["root_folder_id"]:
                raise GoFileError("Guest account response did not include token and root folder")
            self.save_state()
            return
        # Current GoFile API resolves a token via /accounts/getid, then exposes
        # rootFolder on /accounts/{accountId}. The old /accounts/me route is not
        # part of the current API and caused valid keys to be rejected.
        response = requests.get(f"{API_BASE}/accounts/getid", headers=self._headers(), timeout=TIMEOUT)
        account = self._data(response)
        account_id = str(account.get("id") or "")
        if not account_id:
            raise GoFileError("GoFile did not return an account ID for this API key")
        response = requests.get(
            f"{API_BASE}/accounts/{account_id}",
            headers=self._headers(),
            timeout=TIMEOUT,
        )
        data = self._data(response)
        self.state["root_folder_id"] = str(data.get("rootFolder") or "")
        if not self.state["root_folder_id"]:
            raise GoFileError("GoFile account response did not include its root folder")
        self.save_state()

    def create_public_folder(self) -> tuple[str, str]:
        self.ensure_account()
        number = int(self.state.get("folder_number", 0)) + 1
        payload = {
            "parentFolderId": self.state["root_folder_id"],
            "folderName": f"TMJDLC Batch {number:03d}",
            "public": True,
        }
        response = requests.post(
            f"{API_BASE}/contents/createFolder",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=TIMEOUT,
        )
        data = self._data(response)
        folder_id = str(data.get("id") or "")
        folder_code = str(data.get("code") or "")
        if not folder_id or not folder_code:
            raise GoFileError("GoFile did not return a folder ID and public share code")
        self.state.update({
            "folder_id": folder_id,
            "folder_code": folder_code,
            "folder_number": number,
            "video_count": 0,
        })
        self.save_state()
        return folder_id, folder_code

    def upload(
        self, file_path: str | Path, *, folder_id: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        self.ensure_account()
        target_folder_id = str(folder_id or self.state.get("folder_id") or "")
        if not target_folder_id:
            target_folder_id, _ = self.create_public_folder()
        path = Path(file_path)
        if not path.is_file():
            raise GoFileError(f"Upload file does not exist: {path}")
        with path.open("rb") as stream:
            encoder = MultipartEncoder(fields={
                "folderId": target_folder_id,
                "file": (path.name, stream, "application/octet-stream"),
            })
            monitor = MultipartEncoderMonitor(
                encoder,
                lambda current: progress_callback(current.bytes_read, current.len)
                if progress_callback else None,
            )
            response = requests.post(
                UPLOAD_URL,
                headers={**self._headers(), "Content-Type": monitor.content_type},
                data=monitor,
                timeout=(30, 3600),
            )
        data = self._data(response)
        return data

    def current_folder_link(self) -> str:
        code = str(self.state.get("folder_code") or "")
        return f"https://gofile.io/d/{code}" if code else ""
