"""Bounded Drive pipeline with live Telegram transfer progress."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

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


def _size(value: int | float | None) -> str:
    if value is None:
        return "?"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.2f} {unit}"
        number /= 1024
    return "?"


def _eta(value: float | int | None) -> str:
    if value is None or value < 0:
        return "Calculating…"
    seconds = int(value)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h {minutes:02}m" if hours else (f"{minutes}m {seconds:02}s" if minutes else f"{seconds}s")


def _card(code: str, phase: str, done: int, total: int | None,
          speed: float | None, eta: float | None, detail: str = "") -> str:
    if total and total > 0:
        percent = min(100.0, max(0.0, done * 100 / total))
        blocks = min(10, int(percent / 10))
        bar = "█" * blocks + "░" * (10 - blocks)
        size_line, pct = f"{_size(done)} / {_size(total)}", f"{percent:.1f}%"
        if eta is None and speed and speed > 0:
            eta = max(0, total - done) / speed
    else:
        bar, pct, size_line = "░" * 10, "Preparing…" if not done else "Size unknown", _size(done)
    speed_line = f"{_size(speed)}/s" if speed and speed > 0 else "Calculating…"
    emoji = "⬇️" if phase == "DOWNLOAD" else "☁️"
    return (f"{emoji} <b>{phase}</b> · <b>{code}</b>\n"
            f"<code>[{bar}]</code>  <b>{pct}</b>\n"
            f"📦 {size_line}\n⚡ {speed_line}   ⏳ ETA: {_eta(eta)}"
            + (f"\n{detail}" if detail else ""))


class AdjustableGate:
    """A concurrency gate whose limit can be changed while workers are running."""
    def __init__(self, limit: int):
        if not 1 <= limit <= 6:
            raise ValueError("Download concurrency must be between 1 and 6")
        self.limit, self.active = limit, 0
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
    """Download to temporary Colab storage, then upload through a bounded queue."""
    def __init__(self, settings: Settings, drive: DriveManager, store: JsonStore, bot: Any):
        self.settings, self.drive, self.store, self.bot = settings, drive, store, bot
        self.download_queue: asyncio.Queue[QueueJob | None] = asyncio.Queue(maxsize=12)
        self.upload_queue: asyncio.Queue[UploadJob | None] = asyncio.Queue(
            maxsize=max(1, settings.max_parallel_downloads))
        self.download_gate = AdjustableGate(settings.max_parallel_downloads)
        self.tasks: list[asyncio.Task] = []
        self.active_codes: set[str] = set()
        self._codes_lock = asyncio.Lock()

    async def start(self) -> None:
        if self.tasks:
            return
        self.tasks = [
            asyncio.create_task(self._download_worker(), name=f"download-{i+1}") for i in range(6)
        ] + [
            asyncio.create_task(self._upload_worker(), name=f"upload-{i+1}")
            for i in range(self.settings.max_parallel_uploads)
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def set_download_limit(self, limit: int) -> None:
        await self.download_gate.set_limit(limit)

    async def submit(self, chat_id: int, items: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
        queued, skipped = [], []
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
                job = QueueJob(uuid.uuid4().hex[:12], url, code, chat_id)
                self.active_codes.add(code)
                self._save_job(job, "queued")
                self.download_queue.put_nowait(job)
                queued.append(job.job_id)
        return queued, skipped

    def _save_job(self, job: QueueJob, status: str, **extra: Any) -> None:
        def mutate(data: dict[str, Any]) -> None:
            data.setdefault("jobs", {}).setdefault(job.job_id, {}).update(
                {"job_id": job.job_id, "code": job.code, "url": job.url,
                 "status": status, "chat_id": job.chat_id, **extra})
        self.store.update(mutate)

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.exception("Unable to send Telegram notification")

    async def _tracked_transfer(self, job: QueueJob, phase: str, operation: Callable,
                                total: int | None = None):
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        started = time.monotonic()
        try:
            message = await self.bot.send_message(
                chat_id=job.chat_id,
                text=_card(job.code, phase, 0, total, None, None, "Starting…"),
                parse_mode="HTML")
        except Exception:
            log.exception("Could not create progress message for %s", job.code)
            message = None

        def report(data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(events.put_nowait, dict(data))

        worker = asyncio.create_task(asyncio.to_thread(operation, report))
        latest: dict[str, Any] = {}
        last_edit = 0.0

        async def update(data: dict[str, Any], force: bool = False) -> None:
            nonlocal last_edit
            if message is None:
                return
            now = time.monotonic()
            if not force and now - last_edit < 2.0:
                return
            done = int(data.get("downloaded_bytes") or data.get("uploaded_bytes") or data.get("done") or 0)
            size = data.get("total_bytes") or data.get("total") or total
            speed = data.get("speed")
            if speed is None and done:
                speed = done / max(0.1, now - started)
            eta = data.get("eta")
            if eta is None and size and speed and speed > 0:
                eta = max(0, size - done) / speed
            try:
                await message.edit_text(
                    _card(job.code, phase, done, size, speed, eta, data.get("message", "")),
                    parse_mode="HTML")
                last_edit = now
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    log.debug("Progress edit failed: %s", exc)

        while not worker.done():
            try:
                latest = await asyncio.wait_for(events.get(), timeout=0.75)
                await update(latest)
            except asyncio.TimeoutError:
                continue
        while not events.empty():
            latest = events.get_nowait()
        result = await worker
        await update(latest, force=True)
        return result, message

    async def _download_worker(self) -> None:
        while True:
            job = await self.download_queue.get()
            try:
                if job is None:
                    return
                await self.download_gate.acquire()
                try:
                    self._save_job(job, "downloading")
                    def download(progress):
                        engine = DownloadEngine(self.settings.work_dir, progress_callback=progress)
                        return engine.download(job.url, job_id=job.job_id)
                    result, _ = await self._tracked_transfer(job, "DOWNLOAD", download)
                except Exception as exc:
                    self._save_job(job, "failed", error=str(exc)[:500])
                    async with self._codes_lock:
                        self.active_codes.discard(job.code)
                    await self._notify(job.chat_id, f"❌ <b>{job.code}</b> download failed.\n{str(exc)[:600]}")
                    continue
                finally:
                    await self.download_gate.release()
                self._save_job(job, "downloaded", filename=result.filename, size=result.size)
                await self.upload_queue.put(UploadJob(job, result))
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
                    folder = await asyncio.to_thread(self.drive.ensure_code_folder, job.code)
                    def upload(progress):
                        return self.drive.upload_file(
                            result.filepath, folder["id"],
                            progress_callback=lambda done, total: progress(
                                {"uploaded_bytes": done, "total": total}))
                    uploaded, progress_message = await self._tracked_transfer(
                        job, "UPLOAD", upload, total=result.size)
                    if int(uploaded.get("size", -1)) != result.size:
                        raise IOError("Drive upload size verification failed; local file retained.")
                    self.store.record_video(job.code, folder_id=folder["id"],
                        folder_url=folder["url"], filename=result.filename, size=result.size)
                    library = self.store.read()
                    entries = {code: value["folder_url"] for code, value in library["videos"].items()
                               if value.get("folder_url")}
                    await asyncio.to_thread(self.drive.write_index, entries)
                    await asyncio.to_thread(
                        DownloadEngine(self.settings.work_dir).cleanup_job, job.job_id)
                    self._save_job(job, "completed", filename=result.filename, size=result.size,
                        folder_url=folder["url"], drive_file_id=uploaded["id"])
                    final = (f"✅ <b>{job.code} completed</b>\n"
                             f"📦 Size: {_size(result.size)}\n"
                             f"☁️ <a href=\"{folder['url']}\">Open Drive folder</a>")
                    if progress_message:
                        try:
                            await progress_message.edit_text(final, parse_mode="HTML",
                                disable_web_page_preview=True)
                        except Exception:
                            await self._notify(job.chat_id, final)
                    else:
                        await self._notify(job.chat_id, final)
                except Exception as exc:
                    self._save_job(job, "upload_failed", error=str(exc)[:500])
                    await self._notify(job.chat_id,
                        f"⚠️ <b>{job.code}</b> downloaded, but Drive upload failed. "
                        f"Temporary file retained.\n{str(exc)[:600]}")
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
        lines.append(f"Download limit: {self.download_gate.limit} | Upload workers: {self.settings.max_parallel_uploads}")
        return "\n".join(lines)
