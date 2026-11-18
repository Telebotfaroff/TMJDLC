from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


class JsonStore:
    """Small JSON library with atomic replacement and process/thread locking."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._thread_lock = threading.RLock()

    def _locked(self):
        class Lock:
            def __init__(inner, store):
                inner.store = store
                inner.handle = None

            def __enter__(inner):
                inner.store._thread_lock.acquire()
                inner.handle = inner.store.lock_path.open("a+")
                if fcntl:
                    fcntl.flock(inner.handle.fileno(), fcntl.LOCK_EX)
                return inner

            def __exit__(inner, *_):
                if fcntl:
                    fcntl.flock(inner.handle.fileno(), fcntl.LOCK_UN)
                inner.handle.close()
                inner.store._thread_lock.release()
        return Lock(self)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "videos": {}, "jobs": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            if not self.backup_path.exists():
                raise
            data = json.loads(self.backup_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("videos", {}), dict):
            raise ValueError("Invalid TMJDLC database structure")
        data.setdefault("version", 1)
        data.setdefault("videos", {})
        data.setdefault("jobs", {})
        return data

    def read(self) -> dict[str, Any]:
        with self._locked():
            return self._read_unlocked()

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        with self._locked():
            data = self._read_unlocked()
            mutator(data)
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.path.exists():
                    shutil.copy2(self.path, self.backup_path)
                os.replace(temporary, self.path)
                try:
                    directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except (AttributeError, OSError):
                    pass
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return data

    def contains_code(self, code: str) -> bool:
        key = code.strip().upper()
        return bool(key) and key in self.read()["videos"]

    def record_video(self, code: str, *, folder_id: str, folder_url: str, filename: str, size: int) -> None:
        key = code.strip().upper()
        if not key:
            raise ValueError("Video code cannot be empty")
        if size < 0:
            raise ValueError("File size cannot be negative")

        def mutate(data):
            data["videos"][key] = {
                "folder_id": folder_id,
                "folder_url": folder_url,
                "filename": filename,
                "size": size,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        self.update(mutate)
