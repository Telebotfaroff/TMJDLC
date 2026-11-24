"""GitHub-backed durable storage for TMJDLC configuration and database snapshots."""
from __future__ import annotations

import base64
import gzip
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


class GitHubStoreError(RuntimeError):
    """Raised when the private TMJDLC data repository cannot be read or written."""


class GitHubDataStore:
    """Small GitHub Contents-API store; designed for one active Colab runtime."""

    DEFAULT_REPO = "gitroff/Database"
    DEFAULT_BRANCH = "main"
    SNAPSHOT_PATH = "database/tmjdlc.sqlite3.gz"

    def __init__(
        self,
        token: str,
        repo: str = DEFAULT_REPO,
        branch: str = DEFAULT_BRANCH,
        *,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.token = str(token or "").strip()
        if not self.token:
            raise GitHubStoreError("GITHUB_TOKEN is required.")
        self.repo = str(repo or self.DEFAULT_REPO).strip()
        self.branch = str(branch or self.DEFAULT_BRANCH).strip()
        self.timeout = max(5, int(timeout))
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TMJDLC-Colab",
        })
        self.base_url = f"https://api.github.com/repos/{self.repo}"
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls) -> "GitHubDataStore":
        return cls(
            os.getenv("GITHUB_TOKEN", ""),
            os.getenv("GITHUB_DATA_REPO", cls.DEFAULT_REPO),
            os.getenv("GITHUB_DATA_BRANCH", cls.DEFAULT_BRANCH),
        )

    def _url(self, path: str) -> str:
        clean = str(path).strip().lstrip("/")
        if not clean or ".." in Path(clean).parts:
            raise ValueError("Invalid GitHub data path.")
        return f"{self.base_url}/contents/{quote(clean, safe='/')}"

    def _get_file(self, path: str) -> dict[str, Any] | None:
        response = self.session.get(
            self._url(path), params={"ref": self.branch}, timeout=self.timeout
        )
        if response.status_code == 404:
            return None
        if not response.ok:
            raise GitHubStoreError(
                f"GitHub read failed for {path}: HTTP {response.status_code}."
            )
        payload = response.json()
        if payload.get("type") != "file" or not payload.get("content"):
            raise GitHubStoreError(f"GitHub path is not a readable file: {path}.")
        try:
            raw = base64.b64decode(payload["content"], validate=False)
        except Exception as exc:
            raise GitHubStoreError(f"GitHub returned invalid base64 for {path}.") from exc
        return {"sha": payload.get("sha"), "content": raw}

    def read_bytes(self, path: str) -> bytes | None:
        with self._lock:
            result = self._get_file(path)
            return result["content"] if result else None

    def read_json(self, path: str, default: Any = None) -> Any:
        raw = self.read_bytes(path)
        if raw is None:
            return default
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubStoreError(f"Invalid JSON stored at {path}.") from exc

    def write_bytes(self, path: str, content: bytes, message: str) -> str:
        raw = bytes(content)
        encoded = base64.b64encode(raw).decode("ascii")
        with self._lock:
            for attempt in range(4):
                current = self._get_file(path)
                if current and current["content"] == raw:
                    return str(current.get("sha") or "")
                payload: dict[str, Any] = {
                    "message": message[:120],
                    "content": encoded,
                    "branch": self.branch,
                }
                if current and current.get("sha"):
                    payload["sha"] = current["sha"]
                response = self.session.put(
                    self._url(path), json=payload, timeout=self.timeout
                )
                if response.ok:
                    result = response.json()
                    return str((result.get("content") or {}).get("sha") or "")
                if response.status_code in {409, 422} and attempt < 3:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise GitHubStoreError(
                    f"GitHub write failed for {path}: HTTP {response.status_code}."
                )
        raise GitHubStoreError(f"GitHub write conflicted repeatedly for {path}.")

    def write_json(self, path: str, value: Any, message: str) -> str:
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        return self.write_bytes(path, payload + b"\n", message)

    def bootstrap(self) -> None:
        """Create the expected private-repository layout on first use."""
        defaults = {
            "config/secrets.json": {
                "TG_BOT_TOKEN": "",
                "TG_API_ID": "",
                "TG_API_HASH": "",
                "TG_ADMIN_IDS": "",
                "TG_HELPER_BOT_TOKENS": "",
                "GOFILE_API_TOKEN": "",
            },
            "config/bot.json": {
                "TMJDLC_MAX_PARALLEL": 2,
                "TMJDLC_MAX_PENDING_UPLOADS": 1,
                "TMJDLC_UPLOAD_CONCURRENCY": 1,
                "TMJDLC_AUTO_CLEAN": True,
                "TMJDLC_MIN_FREE_DISK_GB": 3,
            },
            "catalogue/channels.json": {"channels": [], "updated_at": None},
        }
        for path, value in defaults.items():
            if self.read_bytes(path) is None:
                self.write_json(path, value, f"Initialize TMJDLC data: {path}")

    def load_runtime_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        self.bootstrap()
        secrets = self.read_json("config/secrets.json", {})
        settings = self.read_json("config/bot.json", {})
        return (
            secrets if isinstance(secrets, dict) else {},
            settings if isinstance(settings, dict) else {},
        )

    def restore_database(self, destination: str | Path) -> bool:
        compressed = self.read_bytes(self.SNAPSHOT_PATH)
        if compressed is None:
            return False
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(gzip.decompress(compressed))
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise GitHubStoreError("The database snapshot in GitHub is corrupt.") from exc
        return True

    def save_database(self, source: str | Path) -> None:
        path = Path(source)
        if not path.is_file():
            return
        compressed = gzip.compress(path.read_bytes(), compresslevel=6, mtime=0)
        self.write_bytes(
            self.SNAPSHOT_PATH,
            compressed,
            f"TMJDLC database snapshot {datetime.now(timezone.utc).isoformat()}",
        )

    def save_upload_record(self, record: dict[str, Any]) -> str:
        job_id = str(record.get("job_id") or record.get("video_id") or "").strip()
        if not job_id:
            raise ValueError("Upload record requires job_id or video_id.")
        now = datetime.now(timezone.utc)
        path = f"uploads/{now:%Y/%m}/{quote(job_id, safe='-_.')}.json"
        self.write_json(path, record, f"Save completed TMJDLC upload {job_id}")
        return path

    def list_channels(self) -> list[dict[str, Any]]:
        payload = self.read_json("catalogue/channels.json", {"channels": []})
        channels = payload.get("channels", []) if isinstance(payload, dict) else []
        return channels if isinstance(channels, list) else []

    def save_channel(self, channel: dict[str, Any]) -> None:
        with self._lock:
            payload = self.read_json("catalogue/channels.json", {"channels": []})
            channels = payload.get("channels", []) if isinstance(payload, dict) else []
            key = str(channel.get("url") or "").rstrip("/").lower()
            if not key:
                raise ValueError("Channel record requires a URL.")
            updated = [item for item in channels if str(item.get("url") or "").rstrip("/").lower() != key]
            updated.append(channel)
            self.write_json(
                "catalogue/channels.json",
                {"channels": updated, "updated_at": datetime.now(timezone.utc).isoformat()},
                "Update Javtiful channel catalogue",
            )
