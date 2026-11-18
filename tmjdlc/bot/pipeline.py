"""Bounded download/upload pipeline for the Telegram control bot."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tmjdlc.config import Settings
from tmjdlc.downloader.engine import DownloadEngine, DownloadResult
from tmjdlc.drive.manager import DriveManager
from tmjdlc.storage.json_store import JsonStore

log = logging.getLogger(__name__)


@dataclass(slots=True)
class QueueJob:
    job_id: str
    url: str
    code: str
    chat_id: int


@dataclass(slots=True)
class UploadJob:
    job: QueueJob
    result: DownloadResult


class AdjustableGate:
    """A concurrency gate whose limit can be changed while workers are running."""

    def __init__(self, limit: int):
        if not 1 <= limit <= 6:
            raise ValueError("Download concurrency must be between 1 and 6")
        self.limit = limit
        self.active = 0
        self.condition = asyncio.Condition()

    async def set_limit(self, limit: int) -> None:
        if not 1 <= limit <= 6:
            raise ValueError("Download concurrency must be between 1 and 6")
        async with self.condition:
            self.limit = limit
            self.condition.notify_all()

    async def acquire(self) -> None:
        async with self.condition:
            await self.condition.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def release(self) -> None:
        async with self.condition:
            self.active -= 1
            self.condition.notify_all()


class DrivePipeline:
    """Downloads to temporary Colab storage, then uploads through a bounded queue."""

    def __init__(
        self,
        settings: Settings,
        drive: DriveManager,
        store: JsonStore,
        bot: Any,
    ):
        self.settings = settings
        self.drive = drive
        self.store = store
        self.bot = bot
        self.engine = DownloadEngine(settings.work_dir)
        self.download_queue: asyncio.Queue[QueueJob | None] = asyncio.Queue(maxsize=12)
        self.upload_queue: asyncio.Queue[UploadJob | None] = asyncio.Queue(
            maxsize=max(1, settings.max_parallel_downloads)
        )
        self.download_gate = AdjustableGate(settings.max_parallel_downloads)
        self.tasks: list[asyncio.Task] = []
        self.active_codes: set[str] = set()
        self._codes_lock = asyncio.Lock()
        self._stopping = False

    async def start(self) -> None:
        if self.tasks:
            return
        self._stopping = False
        self.tasks = [
            asyncio.create_task(self._download_worker(), name=f"download-{i + 1}")
            for i in range(6)
        ] + [
            asyncio.create_task(self._upload_worker(), name=f"upload-{i + 1}")
            for i in range(self.settings.max_parallel_uploads)
        ]

    async def stop(self) -> None:
        self._stopping = True
        for _ in range(6):
            await self.download_queue.put(None)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def set_download_limit(self, limit: int) -> None:
        await self.download_gate.set_limit(limit)

    async def submit(self, chat_id: int, items: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
        """Queue (URL, catalogue code) pairs; return queued IDs and skipped codes."""
        queued: list[str] = []
        skipped: list[str] = []
        for url, raw_code in items:
            code = raw_code.strip().upper()
            if not code:
                skipped.append("(missing code)")
                continue
            async with self._codes_lock:
                if code in self.active_codes or self.store.contains_code(code):
                    skipped.append(code)
                    continue
                if self.download_queue.full():
                    skipped.append(f"{code} (queue full)")
                    continue
                job_id = uuid.uuid4().hex[:12]
                job = QueueJob(job_id=job_id, url=url, code=code, chat_id=chat_id)
                self.active_codes.add(code)
                self._save_job(job, "queued")
                self.download_queue.put_nowait(job)
                queued.append(job_id)
        return queued, skipped

    def _save_job(self, job: QueueJob, status: str, **extra: Any) -> None:
        def mutate(data: dict[str, Any]) -> None:
            record = data.setdefault("jobs", {}).setdefault(job.job_id, {})
            record.update(
                {
                    "job_id": job.job_id,
                    "code": job.code,
                    "url": job.url,
                    "status": status,
                    "chat_id": job.chat_id,
                    **extra,
                }
            )
        self.store.update(mutate)

    async def _notify(self, chat_id: int, message: str) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=message)
        except Exception:
            log.exception("Unable to send Telegram status message")

    async def _download_worker(self) -> None:
        while True:
            job = await self.download_queue.get()
            try:
                if job is None:
                    return
                await self.download_gate.acquire()
                try:
                    self._save_job(job, "downloading")
                    await self._notify(job.chat_id, f"⬇️ Downloading {job.code}\nJob: {job.job_id}")
                    result = await asyncio.to_thread(
                        self.engine.download, job.url, job_id=job.job_id
                    )
                except Exception as exc:
                    self._save_job(job, "failed", error=str(exc)[:500])
                    async with self._codes_lock:
                        self.active_codes.discard(job.code)
                    await self._notify(job.chat_id, f"❌ {job.code} download failed.\n{str(exc)[:700]}")
                    continue
                finally:
                    await self.download_gate.release()
                self._save_job(job, "downloaded", filename=result.filename, size=result.size)
                await self.upload_queue.put(UploadJob(job=job, result=result))
            finally:
                self.download_queue.task_done()

    async def _upload_worker(self) -> None:
        while True:
            item = await self.upload_queue.get()
            try:
                if item is None:
                    return
                job, result = item.job, item.result
                try:
                    self._save_job(job, "uploading", filename=result.filename, size=result.size)
                    await self._notify(job.chat_id, f"☁️ Uploading {job.code} to Google Drive…")
                    folder = await asyncio.to_thread(self.drive.ensure_code_folder, job.code)
                    uploaded = await asyncio.to_thread(
                        self.drive.upload_file, result.filepath, folder["id"]
                    )
                    if int(uploaded.get("size", -1)) != result.size:
                        raise IOError("Drive upload size verification failed; local file retained.")
                    self.store.record_video(
                        job.code,
                        folder_id=folder["id"],
                        folder_url=folder["url"],
                        filename=result.filename,
                        size=result.size,
                    )
                    library = self.store.read()
                    index_entries = {
                        code: value["folder_url"]
                        for code, value in library["videos"].items()
                        if value.get("folder_url")
                    }
                    await asyncio.to_thread(self.drive.write_index, index_entries)
                    await asyncio.to_thread(self.engine.cleanup_job, job.job_id)
                    self._save_job(
                        job, "completed", filename=result.filename, size=result.size,
                        folder_url=folder["url"], drive_file_id=uploaded["id"]
                    )
                    await self._notify(
                        job.chat_id,
                        f"✅ {job.code} completed\n"
                        f"Size: {result.size / 1024 / 1024:.2f} MiB\n"
                        f"Drive: {folder['url']}",
                    )
                except Exception as exc:
                    self._save_job(job, "upload_failed", error=str(exc)[:500])
                    await self._notify(
                        job.chat_id,
                        f"⚠️ {job.code} downloaded, but Drive upload failed. "
                        f"The temporary file was retained for retry.\n{str(exc)[:600]}",
                    )
                finally:
                    async with self._codes_lock:
                        self.active_codes.discard(job.code)
            finally:
                self.upload_queue.task_done()

    def status_text(self) -> str:
        jobs = self.store.read().get("jobs", {})
        counts: dict[str, int] = {}
        for job in jobs.values():
            state = str(job.get("status", "unknown"))
            counts[state] = counts.get(state, 0) + 1
        order = ("queued", "downloading", "downloaded", "uploading", "completed", "failed", "upload_failed")
        lines = ["📊 TMJDLC queue"]
        lines.extend(f"{state}: {counts[state]}" for state in order if counts.get(state))
        if len(lines) == 1:
            lines.append("No jobs recorded yet.")
        lines.append(
            f"Download limit: {self.download_gate.limit} | "
            f"Upload workers: {self.settings.max_parallel_uploads}"
        )
        return "\n".join(lines)
