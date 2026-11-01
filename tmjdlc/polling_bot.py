"""TMJDLC admin bot: Pyrogram handles every Telegram update and media upload."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from tmjdlc.bot_queue import JobQueue
from tmjdlc.providers.javtiful import JavtifulProvider
from tmjdlc.providers.javtiful.channel import JavtifulChannelScraper

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("tmjdlc.pyrogram")

TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "").strip()
ADMINS = {int(x.strip()) for x in os.getenv("TG_ADMIN_IDS", "").split(",") if x.strip()}
MAX_WORKERS = max(1, min(4, int(os.getenv("TMJDLC_MAX_PARALLEL", "2"))))
UPLOAD_WORKERS = max(1, min(8, int(os.getenv("TMJDLC_UPLOAD_WORKERS", "4"))))
DOWNLOAD_FRAGMENTS = max(1, min(16, int(os.getenv("TMJDLC_DOWNLOAD_FRAGMENTS", "8"))))
POLL_SECONDS = max(2, int(os.getenv("TMJDLC_QUEUE_POLL_SECONDS", "4")))
LEASE = max(900, int(os.getenv("TMJDLC_JOB_LEASE_SECONDS", "3600")))
WORK = Path(os.getenv("TMJDLC_WORK_DIR", "/content/tmjdlc-bot"))
SHOT_COUNT = 10
MIN_FREE_DISK_GB = float(os.getenv("TMJDLC_MIN_FREE_DISK_GB", "3"))
AUTO_CLEAN = os.getenv("TMJDLC_AUTO_CLEAN", "true").strip().lower() in {"1", "true", "yes", "on"}
PROGRESS_STEP = max(1, int(os.getenv("TMJDLC_NOTIFY_PROGRESS_STEP", "10")))
VIDEO_ID = re.compile(r"/video/(\d+)", re.I)
CHANNEL_URL = re.compile(r"https?://(?:www\.)?javtiful\.com/channel/[\w-]+", re.I)

VIDEO_CODE = re.compile(r"(?i)([a-z0-9]{2,}(?:-[a-z0-9]{2,})*-\d{2,})")


def video_title_from_url(url: str, fallback_id: str) -> str:
    """Extract a catalogue code from the video URL slug, falling back safely."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    slug = parts[-1] if parts and parts[-1].lower() != "video" else ""
    match = VIDEO_CODE.search(slug)
    if match:
        return match.group(1).upper()
    if slug and not slug.isdigit():
        return re.sub(r"[-_]+", " ", slug).strip().title()
    return fallback_id


def probe_video(path: Path) -> dict:
    """Read Telegram video attributes from the actual downloaded media."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout or "{}")
    stream = (payload.get("streams") or [{}])[0]
    duration = float((payload.get("format") or {}).get("duration") or 0)
    return {
        "duration": max(0, int(round(duration))),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
    }


def create_video_thumbnail(path: Path, video_id: str, width: int, height: int) -> Path | None:
    """Create a compact JPEG thumbnail suitable for Telegram video uploads."""
    target = path.parent / f"javtiful-{video_id}-thumb.jpg"
    # Preserve aspect ratio and cap the longest edge at 320 px.
    if width > 0 and height > 0:
        ratio = min(320 / width, 320 / height, 1.0)
        thumb_width = max(2, int(width * ratio) // 2 * 2)
        thumb_height = max(2, int(height * ratio) // 2 * 2)
    else:
        thumb_width = thumb_height = 320
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1",
             "-i", str(path), "-frames:v", "1",
             "-vf", f"scale={thumb_width}:{thumb_height}",
             "-q:v", "5", "-y", str(target)],
            capture_output=True, check=True,
        )
        if target.is_file() and 0 < target.stat().st_size <= 200 * 1024:
            return target
        target.unlink(missing_ok=True)
    except (OSError, subprocess.CalledProcessError):
        log.exception("Could not generate Telegram thumbnail for video_id=%s", video_id)
    return None


if not TOKEN or not API_ID or not API_HASH or not ADMINS:
    raise RuntimeError("Set TG_BOT_TOKEN, TG_API_ID, TG_API_HASH and TG_ADMIN_IDS.")

queue = JobQueue(max_attempts=int(os.getenv("TMJDLC_JOB_MAX_ATTEMPTS", "3")))
WORK.mkdir(parents=True, exist_ok=True)
worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
workers: list[asyncio.Task] = []
parallel = MAX_WORKERS
active = 0
condition = asyncio.Condition()
setup_state: dict[int, str] = {}
channel_state: dict[int, dict] = {}
download_state: set[int] = set()

app = Client(
    "tmjdlc_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TOKEN,
    workers=16,
    max_concurrent_transmissions=UPLOAD_WORKERS,
    sleep_threshold=60,
)


def is_admin(message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMINS)


async def deny(message) -> bool:
    if not is_admin(message):
        await message.reply_text("This bot is private.")
        log.warning("Rejected update from user_id=%s", message.from_user.id if message.from_user else None)
        return True
    return False


async def status_edit(chat_id, message_id, text):
    if chat_id and message_id:
        try:
            await app.edit_message_text(chat_id, message_id, text[:4000], parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("Could not edit status message chat_id=%s message_id=%s", chat_id, message_id)


async def _cancelled(job_id: int, identity: str) -> bool:
    return await asyncio.to_thread(queue.is_cancel_requested, job_id, identity)


def _fmt_bytes(value: float | int | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _fmt_eta(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes}m" if hours else (f"{minutes}m {secs}s" if minutes else f"{secs}s")


def _progress_bar(percent: int, width: int = 12) -> str:
    filled = max(0, min(width, round(width * percent / 100)))
    return "█" * filled + "░" * (width - filled)


def _progress_text(stage: str, video_id: str, percent: int, done: float,
                   total: float | None, speed: float | None, eta: float | None,
                   attempt: int) -> str:
    icon = "⬇️" if stage == "Download" else "📤"
    size = _fmt_bytes(total) if total else "size unknown"
    return (f"{icon} <b>{stage}</b> · <code>{video_id}</code>\n"
            f"<code>{_progress_bar(percent)}</code>  <b>{percent}%</b>\n"
            f"📦 {_fmt_bytes(done)} / {size}\n"
            f"⚡ {_fmt_bytes(speed)}/s   ⏳ {_fmt_eta(eta)}\n"
            f"🔁 Attempt {attempt}")


async def _heartbeat(job_id: int, identity: str, lost: asyncio.Event, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
            break
        except asyncio.TimeoutError:
            ok = await asyncio.to_thread(queue.heartbeat, job_id, identity, LEASE)
            if not ok:
                lost.set()
                break


async def cleanup_completed_job(path: Path, video_id: str) -> bool:
    """Delete only this completed job's local media and screenshot artifacts."""
    try:
        if path.exists() and path.is_file():
            path.unlink()
        for candidate in path.parent.glob(f"javtiful-{video_id}*"):
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
        shots_dir = WORK / "screenshots" / video_id
        if shots_dir.exists():
            shutil.rmtree(shots_dir, ignore_errors=True)
        log.info("Auto-cleaned completed job media video_id=%s", video_id)
        return not path.exists() and not shots_dir.exists()
    except OSError:
        log.exception("Auto-clean failed for video_id=%s", video_id)
        return False


async def process_job(job: dict, identity: str):
    jid, vid, url = int(job["id"]), str(job["video_id"]), str(job["post_url"])
    chat_id, message_id = job.get("status_chat_id"), job.get("status_message_id")
    out = WORK / "downloads"
    out.mkdir(parents=True, exist_ok=True)
    lost, stop = asyncio.Event(), asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat(jid, identity, lost, stop))

    def check_stop() -> None:
        if lost.is_set():
            raise RuntimeError("Worker lease was lost; stopping this worker.")
        if queue.is_cancel_requested(jid, identity):
            raise asyncio.CancelledError("Cancellation requested.")

    notify_chat = chat_id or job.get("requested_by")
    if notify_chat and not message_id:
        try:
            notice = await app.send_message(int(notify_chat), f"🆕 Job #{jid} · {vid} queued for processing.")
            chat_id, message_id = notice.chat.id, notice.id
        except Exception:
            log.exception("Could not create Telegram status message for job=%s", jid)

    try:
        download_stage = await asyncio.to_thread(queue.stage_done, jid, "download")
        path = Path(download_stage["path"]) if download_stage and download_stage.get("path") else None
        if not path or not path.is_file() or path.stat().st_size == 0:
            check_stop()
            await status_edit(chat_id, message_id, f"⬇️ Downloading {vid} (attempt {job['attempts']})…")

            last_download_notice = {"percent": -PROGRESS_STEP, "time": 0.0}
            loop = asyncio.get_running_loop()

            def progress_hook(data):
                if queue.is_cancel_requested(jid, identity):
                    raise RuntimeError("Cancellation requested.")
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or data.get("total_bytes_estimate")
                    done = data.get("downloaded_bytes", 0)
                    if total:
                        percent = min(100, int(done * 100 / total))
                        queue.update_progress(jid, identity, "downloading", percent)
                        now = time.monotonic()
                        if (percent >= last_download_notice["percent"] + PROGRESS_STEP
                                or now - last_download_notice["time"] >= 4 or percent >= 100):
                            last_download_notice.update(percent=percent, time=now)
                            if chat_id and message_id:
                                notice = _progress_text(
                                    "Download", vid, percent, done, total,
                                    data.get("speed"), data.get("eta"), int(job["attempts"])
                                )
                                asyncio.run_coroutine_threadsafe(
                                    status_edit(chat_id, message_id, notice), loop
                                )

            path = await asyncio.to_thread(
                lambda: JavtifulProvider(max_attempts=2).download(
                    url, out, f"javtiful-{vid}", quality=job.get("quality"),
                    progress_hook=progress_hook,
                    concurrent_fragments=DOWNLOAD_FRAGMENTS,
                )[0]
            )
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("Downloader returned an empty file.")
            if not await asyncio.to_thread(queue.complete_stage, jid, identity, "download",
                                           {"path": str(path), "size": path.stat().st_size}):
                raise RuntimeError("Could not persist download stage; worker ownership was lost.")
        check_stop()
        video_chat = await asyncio.to_thread(queue.get_setting, "upload_channel")
        image_chat = await asyncio.to_thread(queue.get_setting, "image_channel")
        if not video_chat:
            raise RuntimeError("Set video destination first with /setup or /setupload CHANNEL_ID.")

        # Probe the actual file so Telegram receives duration and frame dimensions.
        media_stage = await asyncio.to_thread(queue.stage_done, jid, "media_probe")
        if not media_stage or not all(media_stage.get(k) for k in ("duration", "width", "height")):
            media_info = await asyncio.to_thread(probe_video, path)
            await asyncio.to_thread(queue.complete_stage, jid, identity, "media_probe",
                                    {**media_info, "size": path.stat().st_size})
        else:
            media_info = {
                "duration": int(media_stage.get("duration") or 0),
                "width": int(media_stage.get("width") or 0),
                "height": int(media_stage.get("height") or 0),
            }
        duration = float(media_info["duration"])
        await asyncio.to_thread(queue.save_media, vid, jid, str(path),
                                path.stat().st_size, duration,
                                media_info["width"] or None, media_info["height"] or None)
        title = video_title_from_url(url, vid)

        if not await asyncio.to_thread(queue.set_status, jid, identity, "uploading",
                                       output_path=str(path), lease_seconds=LEASE):
            raise RuntimeError("Job lease was lost before upload.")

        video_stage = await asyncio.to_thread(queue.stage_done, jid, "video_upload")
        if not video_stage:
            check_stop()
            size_mb = path.stat().st_size / 1048576
            await status_edit(chat_id, message_id, f"📤 Uploading {vid} ({size_mb:.1f} MiB) via Pyrogram MTProto…")
            last_upload_notice = {"percent": -PROGRESS_STEP, "time": time.monotonic(),
                                  "bytes": 0, "speed": 0.0}

            async def upload_progress(current, total, *_):
                if not total:
                    return
                now = time.monotonic()
                elapsed = max(now - last_upload_notice["time"], 0.001)
                instant_speed = max(0, current - last_upload_notice["bytes"]) / elapsed
                last_upload_notice["speed"] = (
                    instant_speed if not last_upload_notice["speed"]
                    else last_upload_notice["speed"] * 0.65 + instant_speed * 0.35
                )
                percent = min(100, int(current * 100 / total))
                if (percent >= last_upload_notice["percent"] + PROGRESS_STEP
                        or elapsed >= 4 or percent >= 100):
                    speed = last_upload_notice["speed"]
                    eta = (total - current) / speed if speed else None
                    await status_edit(chat_id, message_id, _progress_text(
                        "Upload", vid, percent, current, total, speed, eta, int(job["attempts"])
                    ))
                    last_upload_notice.update(percent=percent, time=now, bytes=current)

            thumbnail = await asyncio.to_thread(
                create_video_thumbnail, path, vid, media_info["width"], media_info["height"]
            )
            sent = await app.send_video(
                chat_id=int(video_chat), video=str(path), caption=title,
                duration=media_info["duration"], width=media_info["width"],
                height=media_info["height"], thumb=str(thumbnail) if thumbnail else None,
                supports_streaming=True, parse_mode=ParseMode.DISABLED,
                progress=upload_progress,
            )
            if not await asyncio.to_thread(queue.complete_stage, jid, identity, "video_upload",
                                           {"chat_id": sent.chat.id, "message_id": sent.id}):
                raise RuntimeError("Video sent, but its receipt could not be saved in Neon.")
        else:
            log.info("Skipping completed video upload for job=%s message_id=%s", jid,
                     video_stage.get("message_id"))

        sent_shots = 0
        if image_chat and job.get("upload_screenshots", True):
            shots_dir = WORK / "screenshots" / vid
            shots_dir.mkdir(parents=True, exist_ok=True)
            for i in range(SHOT_COUNT):
                check_stop()
                stage_name = f"screenshot_{i+1:02d}"
                if await asyncio.to_thread(queue.stage_done, jid, stage_name):
                    sent_shots += 1
                    continue
                target = shots_dir / f"{vid}-{i+1:02d}.jpg"
                if not target.is_file():
                    stamp = duration * (i + 1) / (SHOT_COUNT + 1)
                    await asyncio.to_thread(subprocess.run, [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(stamp),
                        "-i", str(path), "-frames:v", "1", "-q:v", "3", "-y", str(target)
                    ], capture_output=True, check=True)
                check_stop()
                sent = await app.send_photo(chat_id=int(image_chat), photo=str(target),
                                            caption=f"{title} | Screenshot {i+1}/{SHOT_COUNT}")
                await asyncio.to_thread(queue.complete_stage, jid, identity, stage_name,
                                        {"chat_id": sent.chat.id, "message_id": sent.id})
                sent_shots += 1
                if chat_id and message_id and (sent_shots % 2 == 0 or sent_shots == SHOT_COUNT):
                    percent = int(sent_shots * 100 / SHOT_COUNT)
                    await status_edit(chat_id, message_id,
                        f"🖼️ <b>Screenshot delivery</b> · <code>{vid}</code>\n"
                        f"<code>{_progress_bar(percent)}</code> <b>{sent_shots}/{SHOT_COUNT}</b>")
        else:
            await asyncio.to_thread(queue.complete_stage, jid, identity, "screenshots_skipped", {})
        check_stop()
        if not await asyncio.to_thread(queue.set_status, jid, identity, "completed",
                                       output_path=str(path), lease_seconds=LEASE):
            raise RuntimeError("Could not mark job completed because worker ownership was lost.")
        await status_edit(chat_id, message_id,
            f"✅ <b>Job completed</b> · <code>#{jid}</code>\n"
            f"🎬 Video: <code>{vid}</code>\n"
            f"🖼️ Screenshots delivered: <b>{sent_shots}</b>\n"
            f"📦 Uploaded: <b>{_fmt_bytes(path.stat().st_size)}</b>\n"
            f"🔁 Attempts used: <b>{job['attempts']}</b>")
        if AUTO_CLEAN:
            cleaned = await cleanup_completed_job(path, vid)
            if cleaned:
                await asyncio.to_thread(queue.clear_media_path, vid)
                await status_edit(chat_id, message_id,
                                  f"✅ Completed {vid}. Screenshots sent: {sent_shots}\n"
                                  "🧹 Local video and screenshot files cleaned.")
            else:
                await status_edit(chat_id, message_id,
                                  f"✅ Completed {vid}. Screenshots sent: {sent_shots}\n"
                                  "⚠️ Upload finished, but some local files could not be cleaned.")
    except asyncio.CancelledError:
        await asyncio.to_thread(queue.set_status, jid, identity, "cancelled",
                                error="Cancelled by administrator.", lease_seconds=LEASE)
        await status_edit(chat_id, message_id, f"🛑 Cancelled {vid}.")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:650]}"
        log.exception("Job failed id=%s video_id=%s", jid, vid)
        current = await asyncio.to_thread(queue.get, jid)
        if current and current.get("worker_id") == identity:
            if current.get("cancel_requested"):
                state = "cancelled"
            else:
                state = "queued" if int(current["attempts"]) < int(current["max_attempts"]) else "failed"
            await asyncio.to_thread(queue.set_status, jid, identity, state,
                                    error=error, lease_seconds=LEASE)
            await status_edit(chat_id, message_id,
                              f"{'⚠️ Requeued' if state == 'queued' else '❌ Failed'} {vid}: {html.escape(error)}")
    finally:
        stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def worker_loop(index: int):
    global active
    identity = f"{worker_id}-{index}"
    while True:
        try:
            async with condition:
                await condition.wait_for(lambda: active < parallel)
                active += 1
            try:
                disk_free = shutil.disk_usage(WORK).free / (1024 ** 3)
                if disk_free < MIN_FREE_DISK_GB:
                    await asyncio.sleep(max(POLL_SECONDS, 15))
                    continue
                job = await asyncio.to_thread(queue.claim, identity, LEASE)
                if job:
                    await process_job(job, identity)
                else:
                    await asyncio.sleep(POLL_SECONDS)
            finally:
                async with condition:
                    active = max(0, active - 1)
                    condition.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Worker %s loop error", index)
            await asyncio.sleep(POLL_SECONDS)


async def start_cmd(client, message):
    if await deny(message): return
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("⬇️ Download Video"), KeyboardButton("📚 Batch Download")],
         [KeyboardButton("📋 Queue"), KeyboardButton("⚙️ Settings")],
         [KeyboardButton("💽 Storage"), KeyboardButton("🖥 Worker Status")]],
        resize_keyboard=True, is_persistent=True,
    )
    await message.reply_text(
        "🎬 <b>TMJDLC · Javtiful Downloader</b>\\nChoose an action or send a Javtiful video URL.",
        reply_markup=keyboard, parse_mode=ParseMode.HTML,
    )
    await panel_cmd(client, message)


async def panel_cmd(client, message):
    if await deny(message): return
    counts = await asyncio.to_thread(queue.counts)
    jobs = await asyncio.to_thread(queue.list_jobs, 5)
    text = "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "No jobs yet."
    recent = "\n".join(f"#{x['id']} {x['video_id']} · {x['status']} · {x.get('current_stage') or '-'}" for x in jobs)
    free = shutil.disk_usage(WORK).free / (1024 ** 3)
    await message.reply_text(
        f"🧭 TMJDLC Dashboard\n\n{text}\n\nDisk free: {free:.1f} GiB\n"
        f"Workers: {parallel}/{MAX_WORKERS}\n\nRecent jobs\n{recent}"[:4000]
    )


async def storage_cmd(client, message):
    if await deny(message): return
    usage = shutil.disk_usage(WORK)
    await message.reply_text(
        f"💽 TMJDLC Storage\nTotal: {usage.total / 1024**3:.1f} GiB\n"
        f"Used: {usage.used / 1024**3:.1f} GiB\nFree: {usage.free / 1024**3:.1f} GiB\n"
        f"New downloads pause below {MIN_FREE_DISK_GB:.1f} GiB free."
    )


def batch_keyboard(uid: int) -> InlineKeyboardMarkup:
    state = channel_state[uid]
    selection = state.get("selection", "new")
    quality = state.get("quality", 720)
    quality_label = "Best" if quality is None else f"{quality}p"
    screenshots = "ON" if state.get("screenshots", True) else "OFF"
    skip = "ON" if state.get("skip_existing", True) else "OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 New only" + (" ✓" if selection == "new" else ""), callback_data="channel:select:new"),
            InlineKeyboardButton("📚 All videos" + (" ✓" if selection == "all" else ""), callback_data="channel:select:all"),
        ],
        [
            InlineKeyboardButton("Latest 10" + (" ✓" if selection == "latest:10" else ""), callback_data="channel:select:latest:10"),
            InlineKeyboardButton("Latest 25" + (" ✓" if selection == "latest:25" else ""), callback_data="channel:select:latest:25"),
            InlineKeyboardButton("Latest 50" + (" ✓" if selection == "latest:50" else ""), callback_data="channel:select:latest:50"),
        ],
        [InlineKeyboardButton("✍️ Custom pages", callback_data="channel:custom_pages")],
        [
            InlineKeyboardButton(("480p ✓" if quality == 480 else "480p"), callback_data="channel:quality:480"),
            InlineKeyboardButton(("720p ✓" if quality == 720 else "720p"), callback_data="channel:quality:720"),
            InlineKeyboardButton(("1080p ✓" if quality == 1080 else "1080p"), callback_data="channel:quality:1080"),
            InlineKeyboardButton(("Best ✓" if quality is None else "Best"), callback_data="channel:quality:best"),
        ],
        [
            InlineKeyboardButton(f"🖼 Screenshots: {screenshots}", callback_data="channel:toggle:screenshots"),
            InlineKeyboardButton(f"⏭ Skip existing: {skip}", callback_data="channel:toggle:skip"),
        ],
        [
            InlineKeyboardButton("✅ Review & queue", callback_data="channel:review"),
            InlineKeyboardButton("✖ Cancel", callback_data="channel:cancel"),
        ],
    ])


def batch_summary(info, state: dict) -> str:
    selection = state.get("selection", "new")
    selection_label = {
        "new": "New videos (skip existing)",
        "all": "All channel pages",
        "latest:10": "Latest 10 videos",
        "latest:25": "Latest 25 videos",
        "latest:50": "Latest 50 videos",
        "custom": f"Pages {state.get('pages', [])}",
    }.get(selection, selection)
    quality = state.get("quality", 720)
    quality_label = "Best available" if quality is None else f"{quality}p"
    return (
        f"📚 <b>{html.escape(info.name)}</b>\n"
        f"Videos: <b>{info.total_videos}</b> · Pages: <b>{info.total_pages}</b>\n\n"
        f"Selection: <b>{html.escape(selection_label)}</b>\n"
        f"Quality: <b>{quality_label}</b>\n"
        f"Screenshots: <b>{'Enabled' if state.get('screenshots', True) else 'Disabled'}</b>\n"
        f"Skip existing: <b>{'Yes' if state.get('skip_existing', True) else 'No'}</b>\n\n"
        "Choose every option below, then press Review & queue."
    )


async def show_batch_menu(message, uid: int):
    state = channel_state[uid]
    await message.edit_text(
        batch_summary(state["info"], state),
        reply_markup=batch_keyboard(uid),
        parse_mode=ParseMode.HTML,
    )


async def batch_cmd(client, message):
    if await deny(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        return await message.reply_text("Usage: /batch CHANNEL_URL")
    url = parts[1].strip()
    status = await message.reply_text("🔎 Inspecting channel…")
    try:
        info = await asyncio.to_thread(JavtifulChannelScraper().inspect, url)
        channel_state[message.from_user.id] = {
            "info": info, "selection": "new", "quality": 720,
            "screenshots": True, "skip_existing": True,
            "status_chat_id": status.chat.id,
        }
        await status.edit_text(
            batch_summary(info, channel_state[message.from_user.id]),
            reply_markup=batch_keyboard(message.from_user.id),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await status.edit_text(f"Channel inspection failed: {type(exc).__name__}: {str(exc)[:350]}")


async def queue_channel_batch(message, uid: int):
    state = channel_state.get(uid)
    if not state:
        return await message.edit_text("Batch session expired. Send /batch CHANNEL_URL again.")
    info = state["info"]
    selection = state.get("selection", "new")
    if selection == "custom":
        pages = state.get("pages", [])
    elif selection.startswith("latest:"):
        count = int(selection.split(":", 1)[1])
        page_count = max(1, (count + 22) // 23)
        pages = list(range(max(1, info.total_pages - page_count + 1), info.total_pages + 1))
    else:
        pages = list(range(1, info.total_pages + 1))
    await message.edit_text("⏳ Collecting selected channel pages…")
    try:
        urls = await asyncio.to_thread(JavtifulChannelScraper().videos_for_pages, info, pages)
        if selection.startswith("latest:"):
            urls = urls[-int(selection.split(":", 1)[1]):]
        items = []
        for url in urls:
            match = VIDEO_ID.search(url)
            if match:
                items.append((
                    match.group(1), url, state.get("quality", 720), uid,
                    state.get("screenshots", True),
                ))
        if not items:
            raise RuntimeError("No video links were found in the selected pages.")
        batch_id = uuid.uuid4().hex[:12]
        added, skipped = await asyncio.to_thread(
            queue.enqueue_many, items, skip_existing=state.get("skip_existing", True),
            batch_id=batch_id,
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛑 Cancel remaining batch jobs", callback_data=f"batch:cancel:{batch_id}")
        ]]) if added else None
        await message.edit_text(
            f"✅ <b>Batch queued</b> · <code>{batch_id}</code>\n"
            f"Channel: {html.escape(info.name)}\n"
            f"Selection: {html.escape(selection)}\nFound: {len(items)}\n"
            f"Added: {added}\nSkipped: {skipped}\nQuality: "
            f"{'Best' if state.get('quality') is None else str(state.get('quality')) + 'p'}\n\n"
            "Use the button below to cancel all unfinished jobs from this batch.",
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await message.edit_text(f"❌ Batch failed: {type(exc).__name__}: {str(exc)[:350]}")
    finally:
        channel_state.pop(uid, None)


async def settings_cmd(client, message):
    if await deny(message): return
    video, images = await asyncio.gather(
        asyncio.to_thread(queue.get_setting, "upload_channel", "Not set"),
        asyncio.to_thread(queue.get_setting, "image_channel", "Not set"),
    )
    await message.reply_text(f"Video destination: {video}\nScreenshot destination: {images}\nWorkers: {parallel}/{MAX_WORKERS}\nLease: {LEASE}s")


async def queue_cmd(client, message):
    if await deny(message): return
    counts, jobs = await asyncio.gather(asyncio.to_thread(queue.counts), asyncio.to_thread(queue.list_jobs, 12))
    summary = "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "No jobs yet."
    recent = "\n".join(f"#{x['id']} · {x['video_id']} · {x['status']} · try {x['attempts']}" for x in jobs)
    await message.reply_text(f"📊 Queue\n{summary}\n\nRecent jobs\n{recent}"[:4000])


async def set_channel(message, key):
    if await deny(message): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply_text("Usage: /setupload -1001234567890")
    value = parts[1].strip()
    await asyncio.to_thread(queue.set_setting, key, value)
    await message.reply_text(f"✅ Saved {value} in Neon.")


async def setupload_cmd(client, message): await set_channel(message, "upload_channel")
async def setimages_cmd(client, message): await set_channel(message, "image_channel")


async def setup_cmd(client, message):
    if await deny(message): return
    setup_state[message.from_user.id] = "upload_channel"
    await message.reply_text("Step 1/2: send your video channel @username or numeric ID. The bot must be an admin there.")


async def queue_single_url(message, url: str):
    match = VIDEO_ID.search(url)
    if not match:
        return await message.reply_text("Expected a Javtiful /video/ URL.")
    status = await message.reply_text("⏳ Adding Javtiful video to the queue…")
    shots = str(await asyncio.to_thread(queue.get_setting, "upload_screenshots", "true")).lower() in {"1", "true", "yes", "on"}
    job, created = await asyncio.to_thread(
        queue.enqueue, match.group(1), url, 720,
        requested_by=message.from_user.id, status_chat_id=status.chat.id,
        status_message_id=status.id, upload_screenshots=shots,
    )
    await status.edit_text(
        f"{'🕒 Added to queue' if created else '⏭ Already exists'} · <code>{match.group(1)}</code>\\n"
        f"Job #{job['id']} · {html.escape(str(job['status']))} · 720p automatic",
        parse_mode=ParseMode.HTML,
    )


async def text_handler(client, message):
    if await deny(message): return
    text = (message.text or "").strip()
    uid = message.from_user.id
    menu = {
        "⬇️ Download Video": "download",
        "📚 Batch Download": "batch",
        "📋 Queue": "queue",
        "⚙️ Settings": "settings",
        "💽 Storage": "storage",
        "🖥 Worker Status": "worker",
    }
    if text in menu:
        action = menu[text]
        if action == "download":
            download_state.add(uid)
            return await message.reply_text("⬇️ Send a Javtiful video URL. Send /cancel to stop.")
        if action == "batch":
            return await message.reply_text("📚 Send a Javtiful channel URL to start batch selection.")
        if action == "queue":
            return await queue_cmd(client, message)
        if action == "settings":
            return await settings_cmd(client, message)
        if action == "storage":
            return await storage_cmd(client, message)
        return await message.reply_text(
            f"🖥 Worker Status\\nWorkers: {parallel}/{MAX_WORKERS}\\nActive: {active}\\n"
            f"Queue polling: {POLL_SECONDS}s\\nFree disk: {shutil.disk_usage(WORK).free / 1024**3:.1f} GiB"
        )
    if uid in download_state:
        download_state.discard(uid)
        if not VIDEO_ID.search(text) or urlparse(text).hostname not in {"javtiful.com", "www.javtiful.com"}:
            return await message.reply_text("Please send a valid Javtiful video URL.")
        return await queue_single_url(message, text)
    batch = channel_state.get(uid)
    if batch and batch.get("awaiting") == "custom_pages":
        if text.upper() == "CANCEL":
            batch.pop("awaiting", None)
            return await message.reply_text("Custom page selection cancelled. Use the batch buttons to choose another option.")
        try:
            pages = []
            for token in text.replace(" ", "").split(","):
                if "-" in token:
                    first, last = (int(x) for x in token.split("-", 1))
                    if first > last:
                        raise ValueError("Ranges must be ascending.")
                    pages.extend(range(first, last + 1))
                else:
                    pages.append(int(token))
            pages = sorted(set(pages))
            info = batch["info"]
            if not pages or min(pages) < 1 or max(pages) > info.total_pages:
                raise ValueError(f"Choose pages from 1 to {info.total_pages}.")
            batch["selection"] = "custom"
            batch["pages"] = pages
            batch.pop("awaiting", None)
            status = await message.reply_text("Custom pages saved.")
            await status.edit_text(
                batch_summary(info, batch), reply_markup=batch_keyboard(uid),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            return await message.reply_text(f"Invalid pages: {str(exc)[:250]}\nExample: 1-5,8")
        return
    stage = setup_state.get(uid)
    if stage:
        if text.upper() == "CANCEL":
            setup_state.pop(uid, None)
            return await message.reply_text("Setup cancelled.")
        if stage == "image_channel" and text.upper() == "SKIP":
            await asyncio.to_thread(queue.set_setting, "image_channel", "")
            setup_state.pop(uid, None)
            return await message.reply_text("✅ Setup complete. Screenshot channel disabled.")
        value = text
        if value.startswith("@"):
            try:
                chat = await app.get_chat(value)
                value = str(chat.id)
            except Exception as exc:
                return await message.reply_text(f"Cannot resolve channel: {exc}")
        if not re.fullmatch(r"-?\d+", value):
            return await message.reply_text("Send a numeric channel ID or public @username.")
        key = "upload_channel" if stage == "upload_channel" else "image_channel"
        await asyncio.to_thread(queue.set_setting, key, value)
        if stage == "upload_channel":
            setup_state[uid] = "image_channel"
            return await message.reply_text("✅ Video channel saved. Step 2/2: send screenshot channel ID/@username, or SKIP.")
        setup_state.pop(uid, None)
        return await message.reply_text("✅ Setup complete. Both channel IDs are saved in Neon.")
    if CHANNEL_URL.fullmatch(text):
        status = await message.reply_text("🔎 Inspecting Javtiful channel…")
        try:
            info = await asyncio.to_thread(JavtifulChannelScraper().inspect, text)
            channel_state[uid] = {
                "info": info, "selection": "new", "quality": 720,
                "screenshots": True, "skip_existing": True,
            }
            await status.edit_text(
                batch_summary(info, channel_state[uid]),
                reply_markup=batch_keyboard(uid), parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            await status.edit_text(f"Channel inspection failed: {type(exc).__name__}: {str(exc)[:400]}")
        return
    if VIDEO_ID.search(text) and urlparse(text).hostname in {"javtiful.com", "www.javtiful.com"}:
        return await queue_single_url(message, text)
    await message.reply_text("Use the menu buttons or send a Javtiful video/channel URL.")


async def callback_handler(client, callback):
    uid = callback.from_user.id
    if uid not in ADMINS:
        return await callback.answer("Not authorized.", show_alert=True)
    data = callback.data or ""
    if data.startswith("batch:cancel:"):
        batch_id = data.rsplit(":", 1)[-1]
        result = await asyncio.to_thread(queue.cancel_batch, batch_id)
        await callback.answer("Batch cancellation processed.")
        return await callback.message.edit_text(
            f"🛑 <b>Batch cancellation requested</b> · <code>{batch_id}</code>\n"
            f"Queued jobs cancelled: <b>{result['cancelled']}</b>\n"
            f"Active jobs stopping: <b>{result['stopping']}</b>\n"
            "Other batches and individual jobs were not changed.",
            parse_mode=ParseMode.HTML,
        )
    state = channel_state.get(uid)
    if data == "channel:cancel":
        channel_state.pop(uid, None)
        await callback.answer("Batch cancelled.")
        return await callback.message.edit_text("🛑 Channel batch cancelled.")
    if not state:
        await callback.answer("Session expired. Send the channel URL again.", show_alert=True)
        return await callback.message.edit_text("Batch session expired. Send /batch CHANNEL_URL again.")
    if data == "channel:custom_pages":
        state["awaiting"] = "custom_pages"
        await callback.answer("Send page numbers in chat.")
        return await callback.message.reply_text(
            f"Send page numbers for {state['info'].name}. Example: 1-5,8\n"
            f"Valid range: 1-{state['info'].total_pages}. Send CANCEL to stop."
        )
    if data.startswith("channel:select:"):
        value = data.removeprefix("channel:select:")
        if value in {"new", "all"}:
            state["selection"] = value
        elif value in {"latest:10", "latest:25", "latest:50"}:
            state["selection"] = value
        else:
            return await callback.answer("Unknown selection.", show_alert=True)
        await callback.answer("Selection updated.")
        return await show_batch_menu(callback.message, uid)
    if data.startswith("channel:quality:"):
        value = data.rsplit(":", 1)[-1]
        state["quality"] = None if value == "best" else int(value)
        await callback.answer("Quality updated.")
        return await show_batch_menu(callback.message, uid)
    if data == "channel:toggle:screenshots":
        state["screenshots"] = not state.get("screenshots", True)
        await callback.answer("Screenshot setting updated.")
        return await show_batch_menu(callback.message, uid)
    if data == "channel:toggle:skip":
        state["skip_existing"] = not state.get("skip_existing", True)
        await callback.answer("Duplicate handling updated.")
        return await show_batch_menu(callback.message, uid)
    if data == "channel:review":
        await callback.answer("Starting batch collection…")
        return await queue_channel_batch(callback.message, uid)
    await callback.answer("Unknown batch action.", show_alert=True)


async def dl_cmd(client, message):
    if await deny(message): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply_text("Usage: /dl https://javtiful.com/video/ID/slug")
    url = parts[1].strip()
    match = VIDEO_ID.search(url)
    if not match or urlparse(url).hostname not in {"javtiful.com", "www.javtiful.com"}:
        return await message.reply_text("Expected a Javtiful video URL.")
    status = await message.reply_text("Adding to durable queue…")
    job, created = await asyncio.to_thread(
        queue.enqueue, match.group(1), url, 720,
        requested_by=message.from_user.id, status_chat_id=status.chat.id,
        status_message_id=status.id,
    )
    await status.edit_text(f"{'🕒 Queued' if created else '⏭ Already exists'} {match.group(1)} (job #{job['id']}, {job['status']}).")


async def cancel_cmd(client, message):
    if await deny(message): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return await message.reply_text("Usage: /cancel JOB_ID")
    result = await asyncio.to_thread(queue.request_cancel, int(parts[1].strip()))
    if result == "cancelled":
        return await message.reply_text("🛑 Job cancelled.")
    if result in {"downloading", "uploading"}:
        return await message.reply_text("🛑 Cancellation requested. The worker will stop at its next safe checkpoint.")
    await message.reply_text("Job not found or already completed.")


async def retry_cmd(client, message):
    if await deny(message): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return await message.reply_text("Usage: /retry JOB_ID")
    old = await asyncio.to_thread(queue.get, int(parts[1].strip()))
    if not old or old["status"] != "failed":
        return await message.reply_text("Only failed jobs can be retried.")
    status = await message.reply_text("Requeueing…")
    job, _ = await asyncio.to_thread(
        queue.enqueue, old["video_id"], old["post_url"], old["quality"],
        requested_by=message.from_user.id, status_chat_id=status.chat.id,
        status_message_id=status.id, upload_screenshots=old.get("upload_screenshots", True),
    )
    await status.edit_text(f"🕒 Requeued {old['video_id']} (job #{job['id']}).")


async def log_update(client, message):
    log.info("Telegram update received | user_id=%s | chat_id=%s | text=%s | admin=%s",
             message.from_user.id if message.from_user else None,
             message.chat.id if message.chat else None,
             (message.text or "")[:120],
             bool(message.from_user and message.from_user.id in ADMINS))


async def on_start(client):
    await asyncio.to_thread(queue.initialize)
    recovered = await asyncio.to_thread(queue.recover_expired)
    log.info("Neon queue ready; recovered expired jobs=%s", recovered)
    global workers
    workers = [asyncio.create_task(worker_loop(i + 1)) for i in range(MAX_WORKERS)]
    me = await client.get_me()
    log.info("Pyrogram MTProto bot started | bot_id=%s | username=@%s | admins=%s",
             me.id, me.username, sorted(ADMINS))


async def on_stop(client):
    for task in workers:
        task.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    log.info("TMJDLC workers stopped.")


app.add_handler(MessageHandler(log_update, filters.all), group=-1)
app.add_handler(MessageHandler(start_cmd, filters.command(["start", "help"]) & filters.private))
app.add_handler(MessageHandler(panel_cmd, filters.command("panel") & filters.private))
app.add_handler(MessageHandler(storage_cmd, filters.command("storage") & filters.private))
app.add_handler(MessageHandler(batch_cmd, filters.command("batch") & filters.private))
app.add_handler(MessageHandler(settings_cmd, filters.command("settings") & filters.private))
app.add_handler(MessageHandler(queue_cmd, filters.command("queue") & filters.private))
app.add_handler(MessageHandler(setup_cmd, filters.command("setup") & filters.private))
app.add_handler(MessageHandler(setupload_cmd, filters.command("setupload") & filters.private))
app.add_handler(MessageHandler(setimages_cmd, filters.command("setimages") & filters.private))
app.add_handler(MessageHandler(dl_cmd, filters.command("dl") & filters.private))
app.add_handler(MessageHandler(cancel_cmd, filters.command("cancel") & filters.private))
app.add_handler(MessageHandler(retry_cmd, filters.command("retry") & filters.private))
app.add_handler(CallbackQueryHandler(callback_handler, filters.regex(r"^(channel:|batch:cancel:)")))
app.add_handler(MessageHandler(text_handler, filters.text & filters.private & ~filters.regex(r"^/")))


async def run_bot():
    await app.start()
    await on_start(app)
    log.info("Starting TMJDLC with Pyrogram only: updates, commands, callbacks and large media uploads.")
    try:
        await idle()
    finally:
        await on_stop(app)
        await app.stop()


def main():
    app.run(run_bot())


if __name__ == "__main__":
    main()
