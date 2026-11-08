from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StateStore:
    """Atomic local JSON persistence; deliberately no database or job queue."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "gofile_token": "",
                "root_folder_id": "",
                "folder_id": "",
                "folder_code": "",
                "folder_number": 0,
                "video_count": 0,
                "folder_reserved_count": 0,
                "completed_folders": [],
                "telegram_offset": 0,
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("State file must contain a JSON object")
            return data
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Cannot read state file {self.path}: {exc}") from exc

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".gofile-state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
