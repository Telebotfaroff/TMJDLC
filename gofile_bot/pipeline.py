from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from tmjdlc.providers.javtiful import JavtifulProvider

LOG = logging.getLogger("tmjdlc.gofile.pipeline")
VIDEOS_PER_FOLDER = 20


class DownloadUploadPipeline:
    """Two download workers feeding two independent GoFile upload workers.

    Work is transient and in-memory only. No database or durable job queue.
    """

    def __init__(
        self,
        gofile,
        state: dict[str, Any],
        save_state: Callable[[], None],
        work_dir: Path,
        on_folder_complete: Callable[[int, str], None],
        *,
        download_workers: int = 2,
        upload_workers: int = 2,
        state_lock=None,
    ) -> None:
        self.gofile = gofile
        self.state = state
        self.save_state = save_state
        self.work_dir = work_dir
        self.on_folder_complete = on_folder_complete
        self.download_pool = ThreadPoolExecutor(
            max_workers=download_workers, thread_name_prefix="javtiful-download"
        )
        self.upload_pool = ThreadPoolExecutor(
            max_workers=upload_workers, thread_name_prefix="gofile-upload"
        )
        self.lock = state_lock or threading.RLock()
        self.active_jobs = 0
        self.folders: dict[str, dict[str, Any]] = {}
        folder_id = str(state.get("folder_id") or "")
        if folder_id:
            reserved = max(
                int(state.get("folder_reserved_count", state.get("video_count", 0))),
                int(state.get("video_count", 0)),
            )
            self.folders[folder_id] = {
                "code": str(state.get("folder_code") or ""),
                "number": int(state.get("folder_number", 0)),
                "reserved": reserved,
                "pending": 0,
                "success": int(state.get("video_count", 0)),
            }

    def submit(
        self,
        url: str,
        progress: Callable[[str], None],
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> Future:
        with self.lock:
            self.active_jobs += 1
        future = self.download_pool.submit(self._download_then_upload, url, progress, progress_hook)
        def release_slot(_future):
            with self.lock:
                self.active_jobs = max(0, self.active_jobs - 1)
        future.add_done_callback(release_slot)
        return future

    def _download_then_upload(self, url, progress, progress_hook):
        provider = JavtifulProvider(progress_callback=progress, max_attempts=2)
        import time

        filename = f"video_{int(time.time() * 1000)}"
        progress("Resolving Javtiful stream…")
        path, _stream = provider.download(
            url, self.work_dir, filename, quality=720, progress_hook=progress_hook
        )
        progress(f"TITLE|{_stream.title or path.stem}")
        progress(f"Download complete: {path.name}. Waiting for GoFile upload…")
        upload_future = self.upload_pool.submit(self._upload_one, path, progress)
        try:
            return upload_future.result()
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOG.warning("Could not remove completed download: %s", path)

    def _reserve_folder(self) -> tuple[str, str, int]:
        with self.lock:
            folder_id = str(self.state.get("folder_id") or "")
            folder = self.folders.get(folder_id) if folder_id else None
            if folder is None or folder["reserved"] >= VIDEOS_PER_FOLDER:
                folder_id, code = self.gofile.create_public_folder()
                folder = {
                    "code": code,
                    "number": int(self.state.get("folder_number", 0)),
                    "reserved": 0,
                    "pending": 0,
                    "success": 0,
                }
                self.folders[folder_id] = folder
                self.state["folder_reserved_count"] = 0
                self.save_state()
            folder["reserved"] += 1
            folder["pending"] += 1
            self.state["folder_reserved_count"] = folder["reserved"]
            self.save_state()
            return folder_id, str(folder["code"]), int(folder["number"])

    def _upload_one(self, path: Path, progress: Callable[[str], None]):
        folder_id, code, number = self._reserve_folder()
        success = False
        try:
            progress(f"Uploading {path.name} to GoFile folder {number:03d}…")
            result = self.gofile.upload(
                path, folder_id=folder_id,
                progress_callback=lambda sent, total: progress(f"UPLOAD|{sent}|{total}"),
            )
            success = True
            progress("GoFile upload completed.")
            return result
        finally:
            completed: tuple[int, str] | None = None
            with self.lock:
                folder = self.folders[folder_id]
                folder["pending"] = max(0, folder["pending"] - 1)
                if success:
                    folder["success"] += 1
                if str(self.state.get("folder_id") or "") == folder_id:
                    self.state["video_count"] = folder["success"]
                should_close = folder["reserved"] >= VIDEOS_PER_FOLDER and folder["pending"] == 0
                if should_close:
                    count = int(folder["success"])
                    link = f"https://gofile.io/d/{code}" if code else ""
                    if count and link:
                        completed = (count, link)
                        self.state.setdefault("completed_folders", []).append(
                            {"count": count, "link": link}
                        )
                    self.folders.pop(folder_id, None)
                    if str(self.state.get("folder_id") or "") == folder_id:
                        self.state.update({
                            "folder_id": "",
                            "folder_code": "",
                            "video_count": 0,
                            "folder_reserved_count": 0,
                        })
                self.save_state()
            if completed:
                self.on_folder_complete(*completed)

    def finish_current_folder(self) -> tuple[int, str, bool]:
        """Close the current partial folder only when its in-flight uploads are done."""
        with self.lock:
            if self.active_jobs:
                return int(self.state.get("video_count", 0)), "", False
            folder_id = str(self.state.get("folder_id") or "")
            if not folder_id:
                return 0, "", True
            folder = self.folders.get(folder_id)
            if folder and folder["pending"]:
                return int(folder["success"]), "", False
            count = int(folder["success"] if folder else self.state.get("video_count", 0))
            code = str(folder["code"] if folder else self.state.get("folder_code") or "")
            link = f"https://gofile.io/d/{code}" if count and code else ""
            if count and link:
                self.state.setdefault("completed_folders", []).append(
                    {"count": count, "link": link}
                )
            self.folders.pop(folder_id, None)
            self.state.update({
                "folder_id": "",
                "folder_code": "",
                "video_count": 0,
                "folder_reserved_count": 0,
            })
            self.save_state()
            return count, link, True

    def shutdown(self) -> None:
        self.download_pool.shutdown(wait=False, cancel_futures=False)
        self.upload_pool.shutdown(wait=False, cancel_futures=False)
