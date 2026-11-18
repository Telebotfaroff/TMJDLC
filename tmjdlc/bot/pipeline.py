"""Bounded Drive pipeline with live Telegram transfer progress."""
from __future__ import annotations

import asyncio
import html
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
    batch_id: str


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
    safe_code = html.escape(code)
    safe_detail = html.escape(str(detail)) if detail else ""
    return (f"{emoji} <b>{phase}</b> · <b>{safe_code}</b>\n"
            f"<code>[{bar}]</code>  <b>{pct}</b>\n"
            f"📦 {size_line}\n⚡ {speed_line}   ⏳ ETA: {_eta(eta)}"
            + (f"\n{safe_detail}" if safe_detail else ""))


class BatchProgress:
    """A single editable Telegram message for every job submitted together."""
    def __init__(self, bot: Any, chat_id: int, codes: list[str]):
        self.bot, self.chat_id = bot, chat_id
        self.items = {code: {"status": "QUEUED", "phase": "", "done": 0,
                             "total": None, "speed": None, "eta": None}
                      for code in codes}
        self.message = None
        self.lock = asyncio.Lock()
        self.last_edit = 0.0

    def render(self) -> str:
        total = len(self.items)
        completed = sum(x["status"] == "COMPLETED" for x in self.items.values())
        failed = sum(x["status"] == "FAILED" for x in self.items.values())
        active = sum(x["status"] in ("DOWNLOADING", "UPLOADING") for x in self.items.values())
        lines = [f"🎬 <b>DOWNLOAD BATCH · {total} VIDEOS</b>", "━━━━━━━━━━━━━━━━━━━━"]
        visible_items = list(self.items.items())[:30]
        for code, item in visible_items:
            status = item["status"]
            if status in ("DOWNLOADING", "UPLOADING"):
                phase = item["phase"]
                done, total_bytes = item["done"], item["total"]
                pct = min(100.0, done * 100 / total_bytes) if total_bytes else 0
                emoji = "⬇️" if phase == "DOWNLOAD" else "☁️"
                speed = f"{_size(item['speed'])}/s" if item["speed"] else "—"
                lines.append(
                    f"{emoji} <b>{html.escape(code)}</b> · {pct:.0f}% "
                    f"· {_size(done)}/{_size(total_bytes)} · {speed} · ETA {_eta(item['eta'])}"
                )
            else:
                icon = {"QUEUED": "⏳", "WAITING_UPLOAD": "📥",
                        "COMPLETED": "✅", "FAILED": "❌"}.get(status, "⏳")
                label = {"QUEUED": "Waiting", "WAITING_UPLOAD": "Waiting for upload",
                         "COMPLETED": "Completed", "FAILED": "Failed"}.get(status, status)
                lines.append(f"{icon} <b>{html.escape(code)}</b> · {label}")
        if total > len(visible_items):
            lines.append(f"… and {total - len(visible_items)} more videos")
        lines += ["━━━━━━━━━━━━━━━━━━━━",
                  f"Active: {active} · Completed: {completed}/{total} · Failed: {failed}"]
        return "\n".join(lines)

    async def start(self) -> None:
        try:
            self.message = await self.bot.send_message(
                chat_id=self.chat_id, text=self.render(), parse_mode="HTML",
                disable_web_page_preview=True)
        except Exception:
            log.exception("Could not create batch progress message")

    async def update(self, code: str, status: str | None = None, **values: Any) -> None:
        async with self.lock:
            item = self.items.get(code)
            if item is None:
                return
            if status:
                item["status"] = status
            item.update(values)
            if self.message is not None:
                now = time.monotonic()
                terminal_change = status in ("COMPLETED", "FAILED", "WAITING_UPLOAD")
                if not terminal_change and now - self.last_edit < 1.2:
                    return
                try:
                    await self.message.edit_text(self.render(), parse_mode="HTML",
                                                 disable_web_page_preview=True)
                    self.last_edit = now
                except Exception as exc:
                    if "message is not modified" not in str(exc).lower():
                        log.warning("Batch progress edit failed: %s", exc)


class AdjustableGate:
    """A concurrency gate whose limit can be changed while workers are running."""
    def __init__(self, limit: int, maximum: int = 6):
        self.maximum = maximum
        if not 1 <= limit <= maximum:
            raise ValueError(f"Concurrency must be between 1 and {maximum}")
        self.limit, self.active = limit, 0
        self.condition = asyncio.Condition()

    async def set_limit(self, limit: int) -> None:
        if not 1 <= limit <= self.maximum:
            raise ValueError(f"Concurrency must be between 1 and {self.maximum}")
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
        saved_upload_limit = store.read().get("settings", {}).get(
            "max_parallel_uploads", settings.max_parallel_uploads)
        try:
            saved_upload_limit = int(saved_upload_limit)
        except (TypeError, ValueError):
            saved_upload_limit = settings.max_parallel_uploads
        if not 1 <= saved_upload_limit <= 4:
            saved_upload_limit = settings.max_parallel_uploads
        self.upload_gate = AdjustableGate(saved_upload_limit, maximum=4)
        self.tasks: list[asyncio.Task] = []
        self.active_codes: set[str] = set()
        self.batches: dict[str, BatchProgress] = {}
        self._codes_lock = asyncio.Lock()

    async def start(self) -> None:
        if self.tasks:
            return
        self.tasks = [
            asyncio.create_task(self._download_worker(), name=f"download-{i+1}") for i in range(6)
        ] + [
            asyncio.create_task(self._upload_worker(), name=f"upload-{i+1}")
            for i in range(4)
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def set_download_limit(self, limit: int) -> None:
        await self.download_gate.set_limit(limit)

    async def set_upload_limit(self, limit: int) -> None:
        await self.upload_gate.set_limit(limit)
        self.store.update(lambda data: data.setdefault("settings", {}).update(
            {"max_parallel_uploads": limit}))

    @property
    def upload_limit(self) -> int:
        return self.upload_gate.limit

    async def submit(self, chat_id: int, items: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
        queued, skipped = [], []
        batch_id = uuid.uuid4().hex[:10]
        pending: list[QueueJob] = []
        for url, raw_code in items:
            code = raw_code.strip().upper()
            if not code:
                skipped.append("(missing code)")
                continue
            async with self._codes_lock:
                if code in self.active_codes or self.store.contains_code(code):
                    skipped.append(code)
                    continue
                if self.download_queue.qsize() + len(pending) >= self.download_queue.maxsize:
                    skipped.append(f"{code} (queue full)")
                    continue
                job = QueueJob(uuid.uuid4().hex[:12], url, code, chat_id, batch_id)
                self.active_codes.add(code)
                self._save_job(job, "queued")
                pending.append(job)
                queued.append(job.job_id)
        if pending:
            tracker = BatchProgress(self.bot, chat_id, [job.code for job in pending])
            self.batches[batch_id] = tracker
            await tracker.start()
            for job in pending:
                self.download_queue.put_nowait(job)
        return queued, skipped

    def _save_job(self, job: QueueJob, status: str, **extra: Any) -> None:
        def mutate(data: dict[str, Any]) -> None:
            data.setdefault("jobs", {}).setdefault(job.job_id, {}).update(
                {"job_id": job.job_id, "code": job.code, "url": job.url,
                 "status": status, "chat_id": job.chat_id, **extra})
        self.store.update(mutate)

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            log.exception("Unable to send Telegram notification")

    async def _tracked_transfer(self, job: QueueJob, phase: str, operation: Callable,
                                total: int | None = None):
        """Run a blocking transfer and publish progress to the batch's shared message."""
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        started = time.monotonic()
        last_sample_at = started
        last_sample_bytes = 0
        last_speed: float | None = None
        tracker = self.batches.get(job.batch_id)
        message = tracker.message if tracker else None
        if tracker:
            await tracker.update(job.code,
                status="DOWNLOADING" if phase == "DOWNLOAD" else "UPLOADING",
                phase=phase, done=0, total=total, speed=None, eta=None)

        def report(data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(events.put_nowait, dict(data))

        worker = asyncio.create_task(asyncio.to_thread(operation, report))

        async def update(data: dict[str, Any]) -> None:
            nonlocal last_sample_at, last_sample_bytes, last_speed
            if tracker is None:
                return
            now = time.monotonic()
            done = int(data.get("downloaded_bytes") or data.get("uploaded_bytes")
                       or data.get("done") or 0)
            raw_total = (data.get("total_bytes") or data.get("total_bytes_estimate")
                         or data.get("filesize") or data.get("filesize_approx")
                         or data.get("total") or total)
            size = int(raw_total) if raw_total else None
            reported_speed = data.get("speed")
            if reported_speed:
                speed = float(reported_speed)
                last_speed = speed
            else:
                elapsed = now - last_sample_at
                delta = max(0, done - last_sample_bytes)
                if elapsed >= 0.25 and delta:
                    instant = delta / elapsed
                    last_speed = instant if last_speed is None else 0.65 * instant + 0.35 * last_speed
                speed = last_speed
            if done >= last_sample_bytes:
                last_sample_bytes, last_sample_at = done, now
            eta = data.get("eta")
            if eta is None and size and speed and speed > 0:
                eta = max(0, size - done) / speed
            await tracker.update(job.code,
                status="DOWNLOADING" if phase == "DOWNLOAD" else "UPLOADING",
                phase=phase, done=done, total=size, speed=speed, eta=eta)

        try:
            while not worker.done():
                try:
                    data = await asyncio.wait_for(events.get(), timeout=0.5)
                    await update(data)
                except asyncio.TimeoutError:
                    continue
            await asyncio.sleep(0)
            while not events.empty():
                await update(events.get_nowait())
            result = await worker
            return result, message
        except Exception as exc:
            if tracker:
                await tracker.update(job.code, status="FAILED")
            try:
                setattr(exc, "_tmjdlc_progress_message", message)
            except Exception:
                pass
            raise

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
                    tracker = self.batches.get(job.batch_id)
                    if tracker:
                        await tracker.update(job.code, status="WAITING_UPLOAD", done=result.size,
                                             total=result.size, speed=None, eta=None)
                except Exception as exc:
                    self._save_job(job, "failed", error=str(exc)[:500])
                    async with self._codes_lock:
                        self.active_codes.discard(job.code)
                    tracker = self.batches.get(job.batch_id)
                    if tracker:
                        await tracker.update(job.code, status="FAILED")
                    elif not getattr(exc, "_tmjdlc_progress_message", None):
                        await self._notify(job.chat_id,
                            f"❌ <b>{html.escape(job.code)}</b> download failed.\\n{html.escape(str(exc)[:600])}")
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
                    uploaded, _ = await self._tracked_transfer(
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
                    tracker = self.batches.get(job.batch_id)
                    if tracker:
                        await tracker.update(job.code, status="COMPLETED", done=result.size,
                                             total=result.size, speed=None, eta=0)
                except Exception as exc:
                    self._save_job(job, "upload_failed", error=str(exc)[:500])
                    tracker = self.batches.get(job.batch_id)
                    if tracker:
                        await tracker.update(job.code, status="FAILED")
                    elif not getattr(exc, "_tmjdlc_progress_message", None):
                        await self._notify(job.chat_id,
                            f"⚠️ <b>{html.escape(job.code)}</b> downloaded, but Drive upload failed. "
                            f"Temporary file retained.\\n{html.escape(str(exc)[:600])}")
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
        lines.append(f"Download limit: {self.download_gate.limit} | Upload workers: {self.upload_gate.limit}/4")
        return "\n".join(lines)
