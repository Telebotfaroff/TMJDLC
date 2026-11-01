"""TMJDLC admin bot: one Pyrogram client handles updates, another handles media uploads."""
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
from pyrogram.enums import ParseMode, ChatMemberStatus, ChatType
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
SCREENSHOT_TOKEN = os.getenv("TG_SCREENSHOT_BOT_TOKEN", "").strip()
API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "").strip()
ADMINS = {int(x.strip()) for x in os.getenv("TG_ADMIN_IDS", "").split(",") if x.strip()}
MAX_WORKERS = max(2, min(4, int(os.getenv("TMJDLC_MAX_PARALLEL", "2"))))
UPLOAD_WORKERS = max(1, min(16, int(os.getenv("TMJDLC_UPLOAD_WORKERS", "8"))))
SCREENSHOT_UPLOAD_WORKERS = max(1, min(8, int(os.getenv("TMJDLC_SCREENSHOT_UPLOAD_WORKERS", "2"))))
DOWNLOAD_FRAGMENTS = max(1, min(16, int(os.getenv("TMJDLC_DOWNLOAD_FRAGMENTS", "8"))))
POLL_SECONDS = max(2, int(os.getenv("TMJDLC_QUEUE_POLL_SECONDS", "4")))
MAX_PENDING_UPLOADS = max(0, min(4, int(os.getenv("TMJDLC_MAX_PENDING_UPLOADS", "1"))))
UPLOAD_QUEUE_POLL_SECONDS = max(1, int(os.getenv("TMJDLC_UPLOAD_QUEUE_POLL_SECONDS", "2")))
LEASE = max(900, int(os.getenv("TMJDLC_JOB_LEASE_SECONDS", "3600")))
WORK = Path(os.getenv("TMJDLC_WORK_DIR", "/content/tmjdlc-bot"))
SHOT_COUNT = 10
MIN_FREE_DISK_GB = float(os.getenv("TMJDLC_MIN_FREE_DISK_GB", "3"))
AUTO_CLEAN = os.getenv("TMJDLC_AUTO_CLEAN", "true").strip().lower() in {"1", "true", "yes", "on"}
PROGRESS_STEP = max(1, int(os.getenv("TMJDLC_NOTIFY_PROGRESS_STEP", "2")))
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


if not TOKEN or not SCREENSHOT_TOKEN or not API_ID or not API_HASH or not ADMINS:
    raise RuntimeError("Set TG_BOT_TOKEN, TG_SCREENSHOT_BOT_TOKEN, TG_API_ID, TG_API_HASH and TG_ADMIN_IDS.")
if SCREENSHOT_TOKEN == TOKEN:
    raise RuntimeError("TG_SCREENSHOT_BOT_TOKEN must belong to a separate Telegram bot.")

queue = JobQueue(max_attempts=int(os.getenv("TMJDLC_JOB_MAX_ATTEMPTS", "3")))
WORK.mkdir(parents=True, exist_ok=True)
worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
workers: list[asyncio.Task] = []
parallel = MAX_WORKERS
active = 0
condition = asyncio.Condition()
# One active MTProto video upload at a time. Jobs waiting for this slot count
# against the durable upload-backlog limit enforced by JobQueue.claim().
video_upload_semaphore = asyncio.Semaphore(1)
setup_state: dict[int, str] = {}
channel_state: dict[int, dict] = {}
download_state: set[int] = set()

app = Client(
    "tmjdlc_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TOKEN,
    workers=16,
    max_concurrent_transmissions=1,
    sleep_threshold=60,
)

# Keep media transfers off the update/command session. Both clients authenticate
# as the same bot, but maintain separate Pyrogram peer caches.
upload_client = Client(
    "tmjdlc_upload",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TOKEN,
    no_updates=True,
    workers=4,
    max_concurrent_transmissions=UPLOAD_WORKERS,
    sleep_threshold=300,
)

# Separate Telegram identity/session dedicated to screenshot delivery.
screenshot_client = Client(
    "tmjdlc_screenshots",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=SCREENSHOT_TOKEN,
    no_updates=True,
    workers=2,
    max_concurrent_transmissions=SCREENSHOT_UPLOAD_WORKERS,
    sleep_threshold=300,
)


async def sync_upload_peer(peer_id: int) -> None:
    """Copy a peer's access-hash record from the update session to upload session."""
    peer = await app.resolve_peer(int(peer_id))
    peer_name = type(peer).__name__
    if peer_name == "InputPeerChannel":
        access_hash, peer_type = int(peer.access_hash), "channel"
    elif peer_name == "InputPeerUser":
        access_hash, peer_type = int(peer.access_hash), "user"
    elif peer_name == "InputPeerChat":
        access_hash, peer_type = 0, "group"
    else:
        raise RuntimeError(f"Unsupported Telegram peer type for upload: {peer_name}")
    await upload_client.storage.update_peers([
        (int(peer_id), access_hash, peer_type, None, None)
    ])


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
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                return
            log.warning("Could not edit status message chat_id=%s message_id=%s: %s",
                        chat_id, message_id, exc)


async def send_video_with_slot(status_chat_id, status_message_id, **kwargs):
    """Serialize video uploads and expose the waiting state to Telegram."""
    await status_edit(
        status_chat_id, status_message_id,
        "⏳ Waiting for the video-upload slot. Download queue is applying backpressure…",
    )
    async with video_upload_semaphore:
        return await upload_client.send_video(**kwargs)


class DestinationError(RuntimeError):
    """Destination is missing, inaccessible, or not writable by this bot."""


async def resolve_destination(value: str, client: Client = app):
    """Resolve a channel using the selected bot and verify its posting rights."""
    raw = str(value or "").strip()
    if not raw:
        raise DestinationError("Video destination is not configured. Open Settings and set the video destination.")
    try:
        chat = await client.get_chat(raw if raw.startswith("@") else int(raw))
    except Exception as first_error:
        chat = None
        if not raw.startswith("@"):
            try:
                target_id = int(raw)
                async for dialog in client.get_dialogs():
                    if dialog.chat.id == target_id:
                        chat = dialog.chat
                        break
            except Exception:
                pass
        if chat is None:
            raise DestinationError(
                "Pyrogram cannot resolve this channel. Add the bot as an administrator, "
                "then configure its public @username. For a private channel, make the bot "
                "receive a channel post so Pyrogram can cache the channel peer."
            ) from first_error
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat.id, me.id)
    except Exception as exc:
        raise DestinationError(
            f"Bot cannot access destination {chat.id}. Add it to the channel as an administrator."
        ) from exc
    if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
        raise DestinationError(f"Bot must be an administrator in destination {chat.id}.")
    if getattr(member, "can_post_messages", True) is False:
        raise DestinationError(f"Bot has no permission to post in destination {chat.id}.")
    return chat


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


def _progress_text(stage: str, video_id: str, title: str, percent: int, done: float,
                   total: float | None, speed: float | None, eta: float | None,
                   attempt: int) -> str:
    icon = "⬇️" if stage == "Download" else "📤"
    size = _fmt_bytes(total) if total else "size unknown"
    safe_title = html.escape(title or video_id)
    return (f"{icon} <b>{stage}</b> · <b>{safe_title}</b>\n"
            f"🎬 ID: <code>{html.escape(video_id)}</code>\n"
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

    title = video_title_from_url(url, vid)
    notify_chat = chat_id or job.get("requested_by")
    if notify_chat and not message_id:
        try:
            notice = await app.send_message(int(notify_chat), f"🆕 Job #{jid} · {vid} queued for processing.")
            chat_id, message_id = notice.chat.id, notice.id
        except Exception:
            log.exception("Could not create Telegram status message for job=%s", jid)

    try:
        # Resolve destinations and warm the dedicated upload session before downloading.
        # This removes Telegram peer lookups from the download-complete -> upload path.
        video_chat_value = await asyncio.to_thread(queue.get_setting, "upload_channel")
        image_chat_value = await asyncio.to_thread(queue.get_setting, "image_channel")

        # A missing/unresolvable channel must not discard a successfully downloaded
        # video. Prefer the configured channel, but fall back directly to the
        # requesting user's private bot chat when peer resolution fails.
        video_destination = None
        try:
            video_destination = await resolve_destination(video_chat_value)
            await sync_upload_peer(video_destination.id)
        except DestinationError as exc:
            fallback_chat_id = int(job.get("requested_by") or 0)
            if not fallback_chat_id:
                raise
            log.warning(
                "Video destination unavailable for job=%s; using private-chat fallback user_id=%s: %s",
                jid, fallback_chat_id, exc,
            )
            await sync_upload_peer(fallback_chat_id)

        # Screenshot delivery is optional; a bad screenshot destination must
        # not block the primary video upload.
        image_destination = None
        if image_chat_value and job.get("upload_screenshots", True):
            try:
                image_destination = await resolve_destination(image_chat_value, screenshot_client)
                await screenshot_client.resolve_peer(image_destination.id)
            except DestinationError as exc:
                log.warning(
                    "Screenshot destination unavailable for job=%s; skipping screenshots: %s",
                    jid, exc,
                )

        # Reuse a previously completed upload receipt without introducing a
        # post-download peer lookup when recovering a job after a retry.
        video_stage = await asyncio.to_thread(queue.stage_done, jid, "video_upload")
        if video_stage and video_stage.get("chat_id"):
            await sync_upload_peer(int(video_stage["chat_id"]))

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
                                or now - last_download_notice["time"] >= 2 or percent >= 100):
                            last_download_notice.update(percent=percent, time=now)
                            if chat_id and message_id:
                                notice = _progress_text(
                                    "Download", vid, title, percent, done, total,
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
        await status_edit(chat_id, message_id,
                          f"✅ Download complete · <code>{html.escape(title)}</code>\n"
                          "⚡ Upload session ready. Preparing Telegram media…")

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
        if not await asyncio.to_thread(queue.set_status, jid, identity, "uploading",
                                       output_path=str(path), lease_seconds=LEASE):
            raise RuntimeError("Job lease was lost before upload.")

        default_upload_chat_id = (
            video_destination.id if video_destination is not None
            else int(job.get("requested_by") or 0)
        )
        if not default_upload_chat_id:
            raise DestinationError("No usable video destination or requesting-user chat is available.")
        upload_chat_id = (
            int(video_stage.get("chat_id") or default_upload_chat_id)
            if video_stage else default_upload_chat_id
        )
        fallback_used = (
            bool(video_stage.get("fallback"))
            if video_stage else video_destination is None
        )
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
                        or elapsed >= 2 or percent >= 100):
                    speed = last_upload_notice["speed"]
                    eta = (total - current) / speed if speed else None
                    await status_edit(chat_id, message_id, _progress_text(
                        "Upload", vid, title, percent, current, total, speed, eta, int(job["attempts"])
                    ))
                    last_upload_notice.update(percent=percent, time=now, bytes=current)

            thumbnail = await asyncio.to_thread(
                create_video_thumbnail, path, vid, media_info["width"], media_info["height"]
            )
            try:
                if video_destination is None:
                    await status_edit(
                        chat_id, message_id,
                        f"⚠️ Channel destination unavailable for <code>{vid}</code>. "
                        "Sending directly to your private bot chat…",
                    )
                await sync_upload_peer(upload_chat_id)
                sent = await send_video_with_slot(
                    chat_id, message_id,
                    chat_id=upload_chat_id, video=str(path), caption=title,
                    duration=media_info["duration"], width=media_info["width"],
                    height=media_info["height"], thumb=str(thumbnail) if thumbnail else None,
                    supports_streaming=True, parse_mode=ParseMode.DISABLED,
                    progress=upload_progress,
                )
            except Exception as channel_error:
                fallback_chat_id = int(job.get("requested_by") or 0)
                if not fallback_chat_id or fallback_chat_id == upload_chat_id:
                    raise
                log.exception(
                    "Channel upload failed for job=%s; trying private bot chat user_id=%s",
                    jid, fallback_chat_id,
                )
                await status_edit(
                    chat_id, message_id,
                    f"⚠️ Channel upload failed for <code>{vid}</code>. "
                    "Trying fallback delivery to your private bot chat…",
                )
                try:
                    await sync_upload_peer(fallback_chat_id)
                    sent = await send_video_with_slot(
                        chat_id, message_id,
                        chat_id=fallback_chat_id, video=str(path), caption=title,
                        duration=media_info["duration"], width=media_info["width"],
                        height=media_info["height"], thumb=str(thumbnail) if thumbnail else None,
                        supports_streaming=True, parse_mode=ParseMode.DISABLED,
                        progress=upload_progress,
                    )
                    upload_chat_id = fallback_chat_id
                    fallback_used = True
                except Exception as fallback_error:
                    raise RuntimeError(
                        "Channel upload failed and private-chat fallback also failed: "
                        f"{type(fallback_error).__name__}: {str(fallback_error)[:350]}"
                    ) from fallback_error
            if not await asyncio.to_thread(queue.complete_stage, jid, identity, "video_upload",
                                           {"chat_id": sent.chat.id, "message_id": sent.id,
                                            "fallback": fallback_used}):
                raise RuntimeError("Video sent, but its receipt could not be saved in Neon.")
        else:
            log.info("Skipping completed video upload for job=%s message_id=%s", jid,
                     video_stage.get("message_id"))

        sent_shots = 0
        if image_destination and job.get("upload_screenshots", True):
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
                sent = await screenshot_client.send_photo(chat_id=image_destination.id, photo=str(target),
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
            f"🔁 Attempts used: <b>{job['attempts']}</b>\n"
            f"{'📩 Delivered to your private bot chat (fallback).' if fallback_used else ''}")
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
            elif isinstance(exc, DestinationError):
                state = "failed"
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
                if str(await asyncio.to_thread(queue.get_setting, "queue_paused", "false")).lower() in {"1", "true", "yes", "on"}:
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                disk_free = shutil.disk_usage(WORK).free / (1024 ** 3)
                if disk_free < MIN_FREE_DISK_GB:
                    await asyncio.sleep(max(POLL_SECONDS, 15))
                    continue
                job = await asyncio.to_thread(
                    queue.claim, identity, LEASE,
                    max_pending_uploads=MAX_PENDING_UPLOADS,
                )
                if job:
                    await process_job(job, identity)
                else:
                    await asyncio.sleep(UPLOAD_QUEUE_POLL_SECONDS)
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
    free = shutil.disk_usage(WORK).free / (1024 ** 3)
    running = counts.get("downloading", 0) + counts.get("uploading", 0)
    body = (
        "🎬 <b>TMJDLC · Javtiful Downloader</b>\\n\\n"
        f"⚡ Active: <b>{running}</b> · 🕒 Queued: <b>{counts.get('queued', 0)}</b>\\n"
        f"✅ Completed: <b>{counts.get('completed', 0)}</b> · ❌ Failed: <b>{counts.get('failed', 0)}</b>\\n"
        f"💽 Free storage: <b>{free:.1f} GiB</b> · ⚙️ Workers: <b>{parallel}/{MAX_WORKERS}</b>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download", callback_data="home:download"),
         InlineKeyboardButton("📚 Batch", callback_data="home:batch")],
        [InlineKeyboardButton("📋 Queue manager", callback_data="home:queue"),
         InlineKeyboardButton("⚙️ Settings", callback_data="home:settings")],
        [InlineKeyboardButton("💽 Storage", callback_data="home:storage"),
         InlineKeyboardButton("🖥 Worker status", callback_data="home:worker")],
    ])
    await message.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


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
    return (
        f"📚 <b>{html.escape(info.name)}</b>\n"
        f"Videos: <b>{info.total_videos}</b> · Pages: <b>{info.total_pages}</b>\n\n"
        f"Selection: <b>{html.escape(selection_label)}</b>\n"
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
                    match.group(1), url, 720, uid,
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
            f"Added: {added}\nSkipped: {skipped}\nQuality: 720p (automatic)\n\n"
            "Use the button below to cancel all unfinished jobs from this batch.",
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await message.edit_text(f"❌ Batch failed: {type(exc).__name__}: {str(exc)[:350]}")
    finally:
        channel_state.pop(uid, None)


def queue_keyboard(jobs, offset: int, paused: bool) -> InlineKeyboardMarkup:
    rows = []
    for job in jobs:
        jid, status = int(job["id"]), str(job["status"])
        label = f"#{jid} · {str(job['video_id'])[:18]} · {status}"
        if status in {"queued", "downloading", "uploading"}:
            rows.append([InlineKeyboardButton("🛑 " + label, callback_data=f"job:cancel:{jid}")])
        elif status == "failed":
            rows.append([InlineKeyboardButton("🔁 " + label, callback_data=f"job:retry:{jid}")])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ Newer", callback_data=f"queue:page:{max(0, offset-10)}"))
    if len(jobs) == 10:
        nav.append(InlineKeyboardButton("Older ➡️", callback_data=f"queue:page:{offset+10}"))
    if nav: rows.append(nav)
    rows.extend([
        [InlineKeyboardButton("⏸ Pause queue" if not paused else "▶️ Resume queue",
                              callback_data="queue:pause" if not paused else "queue:resume"),
         InlineKeyboardButton("🛑 Cancel queued", callback_data="queue:cancel_queued")],
        [InlineKeyboardButton("🚨 Cancel ALL tasks", callback_data="queue:cancel_all:ask")],
        [InlineKeyboardButton("🧹 Clear completed", callback_data="queue:clear:ask"),
         InlineKeyboardButton("🔄 Refresh", callback_data=f"queue:page:{offset}")],
        [InlineKeyboardButton("🏠 Home", callback_data="home:show")],
    ])
    return InlineKeyboardMarkup(rows)


async def render_queue(target, offset: int = 0, edit: bool = False):
    counts, jobs, paused = await asyncio.gather(
        asyncio.to_thread(queue.counts),
        asyncio.to_thread(queue.list_jobs, 10, offset),
        asyncio.to_thread(queue.get_setting, "queue_paused", "false"),
    )
    paused = str(paused).lower() in {"1", "true", "yes", "on"}
    lines = ["📋 <b>Queue Manager</b>", f"Status: <b>{'PAUSED' if paused else 'RUNNING'}</b>", ""]
    for key in ("queued", "downloading", "uploading", "completed", "failed", "cancelled"):
        lines.append(f"{key.title()}: <b>{counts.get(key, 0)}</b>")
    lines.append("")
    lines.append(f"<b>Recent jobs · {offset+1}–{offset+len(jobs)}</b>" if jobs else "No jobs on this page.")
    for job in jobs:
        stage = f" · {html.escape(str(job.get('current_stage') or ''))}" if job.get("current_stage") else ""
        progress = f" · {int(job.get('progress_percent') or 0)}%" if job.get("status") in {"downloading", "uploading"} else ""
        lines.append(f"#{job['id']} <code>{html.escape(str(job['video_id']))}</code> · <b>{html.escape(str(job['status']))}</b>{stage}{progress}")
    body = "\n".join(lines)[:3900]
    markup = queue_keyboard(jobs, offset, paused)
    if edit:
        try:
            return await target.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                return target
            raise
    return await target.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


async def queue_cmd(client, message):
    if await deny(message): return
    return await render_queue(message)


async def settings_cmd(client, message, check_access=True):
    if check_access and await deny(message): return
    video, images, shots, paused = await asyncio.gather(
        asyncio.to_thread(queue.get_setting, "upload_channel", "Not set"),
        asyncio.to_thread(queue.get_setting, "image_channel", "Not set"),
        asyncio.to_thread(queue.get_setting, "upload_screenshots", "true"),
        asyncio.to_thread(queue.get_setting, "queue_paused", "false"),
    )
    enabled = str(shots).lower() in {"1", "true", "yes", "on"}
    paused = str(paused).lower() in {"1", "true", "yes", "on"}
    body = (
        "⚙️ <b>Settings</b>\\n\\n"
        f"🎞 Video destination: <code>{html.escape(str(video))}</code>\\n"
        f"🖼 Screenshot destination: <code>{html.escape(str(images))}</code>\\n"
        f"📸 Screenshot delivery: <b>{'ON' if enabled else 'OFF'}</b>\\n"
        "🎥 Quality: <b>720p automatic</b>\\n"
        f"⚡ Worker concurrency: <b>{parallel}/{MAX_WORKERS}</b>\\n"
        f"📥 Queue processing: <b>{'PAUSED' if paused else 'RUNNING'}</b>\\n"
        f"⏱ Lease: <b>{LEASE}s</b>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📸 Screenshots: {'ON' if enabled else 'OFF'}", callback_data="settings:shots")],
        [InlineKeyboardButton("🎞 Set video destination", callback_data="settings:video"),
         InlineKeyboardButton("🖼 Set screenshot destination", callback_data="settings:images")],
        [InlineKeyboardButton("➖ Worker", callback_data="settings:workers:down"),
         InlineKeyboardButton(f"Workers {parallel}", callback_data="settings:workers:show"),
         InlineKeyboardButton("➕ Worker", callback_data="settings:workers:up")],
        [InlineKeyboardButton("⏸ Pause queue" if not paused else "▶️ Resume queue",
                              callback_data="queue:pause" if not paused else "queue:resume")],
        [InlineKeyboardButton("🏠 Home", callback_data="home:show"),
         InlineKeyboardButton("📋 Queue manager", callback_data="home:queue")],
    ])
    await message.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


async def set_channel(message, key):
    if await deny(message): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        setup_state[message.from_user.id] = key
        label = "video upload" if key == "upload_channel" else "screenshot"
        return await message.reply_text(
            f"Forward any recent post from your private {label} channel here. "
            "The bot will resolve the channel from that forwarded post, verify posting rights, "
            "and save it in Neon. Send CANCEL to stop."
        )
    value = parts[1].strip()
    try:
        target_client = screenshot_client if key == "image_channel" else app
        chat = await resolve_destination(value, target_client)
    except DestinationError as exc:
        return await message.reply_text(
            f"❌ {html.escape(str(exc))}\n\n"
            "For a private channel, run this command without an ID and forward a post from the target channel.",
            parse_mode=ParseMode.HTML,
        )
    await asyncio.to_thread(queue.set_setting, key, str(chat.id))
    await message.reply_text(
        f"✅ Verified administrator access and saved channel <code>{chat.id}</code> in Neon.",
        parse_mode=ParseMode.HTML,
    )


async def setupload_cmd(client, message): await set_channel(message, "upload_channel")
async def setimages_cmd(client, message): await set_channel(message, "image_channel")


def forwarded_source_chat(message):
    source = getattr(message, "forward_from_chat", None)
    if source is not None:
        return source
    origin = getattr(message, "forward_origin", None)
    return getattr(origin, "chat", None) if origin is not None else None


async def forwarded_destination_handler(client, message):
    if not message.from_user or message.from_user.id not in ADMINS:
        return
    key = setup_state.get(message.from_user.id)
    if key not in {"upload_channel", "image_channel"}:
        return
    source = forwarded_source_chat(message)
    if source is None or getattr(source, "type", None) != ChatType.CHANNEL:
        return await message.reply_text(
            "I couldn't read the channel origin. Forward a normal post from the private channel "
            "(use Telegram Forward, not copy/paste), or send CANCEL."
        )
    try:
        # Resolve and verify with the bot that will actually publish to this channel.
        target_client = screenshot_client if key == "image_channel" else app
        chat = await target_client.get_chat(source.id)
        verified = await resolve_destination(str(chat.id), target_client)
    except Exception as exc:
        return await message.reply_text(
            f"❌ Could not verify forwarded channel: {html.escape(str(exc)[:450])}\n"
            "Confirm the bot is an administrator with permission to post, then forward a fresh post."
        )
    if key == "image_channel":
        await screenshot_client.resolve_peer(verified.id)
    else:
        await sync_upload_peer(verified.id)
    await asyncio.to_thread(queue.set_setting, key, str(verified.id))
    setup_state.pop(message.from_user.id, None)
    label = "Video" if key == "upload_channel" else "Screenshot"
    await message.reply_text(
        f"✅ {label} channel verified and saved: <code>{verified.id}</code>\n"
        f"Title: {html.escape(verified.title or 'Private channel')}",
        parse_mode=ParseMode.HTML,
    )


async def channel_post_peer_cache(client, message):
    # Incoming channel_post updates seed Pyrogram's local peer/access-hash cache.
    try:
        await app.get_chat(message.chat.id)
        await sync_upload_peer(message.chat.id)
        log.info("Channel peer cached in both sessions | channel_id=%s | title=%s",
                 message.chat.id, getattr(message.chat, "title", None))
    except Exception:
        log.exception("Could not cache peer from channel post | channel_id=%s", message.chat.id)


async def setup_cmd(client, message):
    if await deny(message): return
    setup_state[message.from_user.id] = "upload_channel"
    await message.reply_text(
        "Step 1/2: forward any recent post from your private video channel here. "
        "The bot must be an administrator with permission to post."
    )


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
        if not re.fullmatch(r"-?\d+", value) and not re.fullmatch(r"@[A-Za-z0-9_]{5,}", value):
            return await message.reply_text(
                "For private channels, forward a channel post instead of typing its numeric ID. "
                "For public channels, send @username."
            )
        try:
            chat = await resolve_destination(value)
        except DestinationError as exc:
            return await message.reply_text(f"❌ {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
        value = str(chat.id)
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
    global parallel
    if data == "home:show":
        counts = await asyncio.to_thread(queue.counts)
        free = shutil.disk_usage(WORK).free / (1024 ** 3)
        running = counts.get("downloading", 0) + counts.get("uploading", 0)
        body = ("🎬 <b>TMJDLC · Javtiful Downloader</b>\\n\\n"
                f"⚡ Active: <b>{running}</b> · 🕒 Queued: <b>{counts.get('queued', 0)}</b>\\n"
                f"✅ Completed: <b>{counts.get('completed', 0)}</b> · ❌ Failed: <b>{counts.get('failed', 0)}</b>\\n"
                f"💽 Free storage: <b>{free:.1f} GiB</b>\\n"
                f"⚙️ Workers: <b>{parallel}/{MAX_WORKERS}</b>")
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇️ Download", callback_data="home:download"),
             InlineKeyboardButton("📚 Batch", callback_data="home:batch")],
            [InlineKeyboardButton("📋 Queue manager", callback_data="home:queue"),
             InlineKeyboardButton("⚙️ Settings", callback_data="home:settings")],
            [InlineKeyboardButton("💽 Storage", callback_data="home:storage"),
             InlineKeyboardButton("🖥 Worker status", callback_data="home:worker")],
        ])
        await callback.answer()
        return await callback.message.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)
    if data == "home:queue":
        await callback.answer()
        return await render_queue(callback.message, edit=True)
    if data == "home:settings":
        await callback.answer()
        return await settings_cmd(client, callback.message, check_access=False)
    if data == "home:download":
        download_state.add(uid)
        await callback.answer("Send a Javtiful video URL.")
        return await callback.message.reply_text("⬇️ Send a Javtiful video URL. Downloads use 720p automatically.")
    if data == "home:batch":
        await callback.answer("Send a Javtiful channel URL.")
        return await callback.message.reply_text("📚 Send a Javtiful channel URL to open batch selection.")
    if data == "home:storage":
        usage = shutil.disk_usage(WORK)
        await callback.answer()
        return await callback.message.edit_text(
            f"💽 <b>Storage</b>\\nTotal: {usage.total/1024**3:.1f} GiB\\n"
            f"Used: {usage.used/1024**3:.1f} GiB\\nFree: {usage.free/1024**3:.1f} GiB\\n"
            f"New downloads pause below {MIN_FREE_DISK_GB:.1f} GiB free.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home:show")]]),
            parse_mode=ParseMode.HTML)
    if data == "home:worker":
        await callback.answer()
        return await callback.message.edit_text(
            f"🖥 <b>Worker Status</b>\\nWorkers: {parallel}/{MAX_WORKERS}\\nActive: {active}\\n"
            f"Queue polling: {POLL_SECONDS}s\\nLease: {LEASE}s",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home:show")]]),
            parse_mode=ParseMode.HTML)
    if data == "settings:shots":
        current = str(await asyncio.to_thread(queue.get_setting, "upload_screenshots", "true")).lower() in {"1", "true", "yes", "on"}
        await asyncio.to_thread(queue.set_setting, "upload_screenshots", "false" if current else "true")
        await callback.answer(f"Screenshots {'disabled' if current else 'enabled'}.")
        return await settings_cmd(client, callback.message)
    if data in {"settings:video", "settings:images"}:
        setup_state[uid] = "upload_channel" if data == "settings:video" else "image_channel"
        await callback.answer()
        label = "video destination" if data == "settings:video" else "screenshot destination"
        return await callback.message.reply_text(
            f"Forward any recent post from the private {label} channel here. "
            "The bot will resolve and verify the channel peer before saving it. Send CANCEL to stop."
        )
    if data.startswith("settings:workers:"):
        direction = data.rsplit(":", 1)[-1]
        if direction == "up":
            parallel = min(MAX_WORKERS, parallel + 1)
        elif direction == "down":
            parallel = max(2, parallel - 1)
        async with condition:
            condition.notify_all()
        await callback.answer(f"Worker concurrency: {parallel}")
        return await settings_cmd(client, callback.message)
    if data == "queue:pause":
        await asyncio.to_thread(queue.set_setting, "queue_paused", "true")
        await callback.answer("Queue paused. Active jobs will finish.")
        return await render_queue(callback.message, edit=True)
    if data == "queue:resume":
        await asyncio.to_thread(queue.set_setting, "queue_paused", "false")
        await callback.answer("Queue resumed.")
        return await render_queue(callback.message, edit=True)
    if data == "queue:cancel_queued":
        count = await asyncio.to_thread(queue.cancel_queued)
        await callback.answer(f"Cancelled {count} queued jobs.", show_alert=True)
        return await render_queue(callback.message, edit=True)
    if data == "queue:cancel_all:ask":
        counts = await asyncio.to_thread(queue.counts)
        await callback.answer()
        return await callback.message.edit_text(
            "🚨 <b>Cancel ALL unfinished tasks?</b>\\n"
            f"Queued: <b>{counts.get('queued', 0)}</b>\\n"
            f"Downloading: <b>{counts.get('downloading', 0)}</b>\\n"
            f"Uploading: <b>{counts.get('uploading', 0)}</b>\\n\\n"
            "This affects every batch and individual job. Active transfers will stop at their next safe checkpoint. "
            "Completed jobs will not be changed.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚨 Yes, cancel ALL", callback_data="queue:cancel_all:confirm"),
                 InlineKeyboardButton("↩️ Go back", callback_data="home:queue")]
            ]),
            parse_mode=ParseMode.HTML,
        )
    if data == "queue:cancel_all:confirm":
        result = await asyncio.to_thread(queue.cancel_all)
        await callback.answer("All unfinished tasks cancellation requested.", show_alert=True)
        return await callback.message.edit_text(
            "🛑 <b>Cancel all requested</b>\\n"
            f"Queued cancelled: <b>{result['cancelled']}</b>\\n"
            f"Active tasks stopping: <b>{result['stopping']}</b>\\n"
            "Completed tasks were left untouched.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Open queue", callback_data="home:queue"),
                 InlineKeyboardButton("🏠 Home", callback_data="home:show")]
            ]),
            parse_mode=ParseMode.HTML,
        )
    if data == "queue:clear:ask":
        await callback.answer()
        return await callback.message.edit_text(
            "🧹 <b>Clear completed history?</b>\\nCompleted jobs still referenced by the media catalogue will be retained.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes, clear history", callback_data="queue:clear:confirm"),
                 InlineKeyboardButton("No, go back", callback_data="home:queue")]]),
            parse_mode=ParseMode.HTML)
    if data == "queue:clear:confirm":
        count = await asyncio.to_thread(queue.clear_completed_history)
        await callback.answer(f"Removed {count} completed history rows.", show_alert=True)
        return await render_queue(callback.message, edit=True)
    if data.startswith("queue:page:"):
        offset = int(data.rsplit(":", 1)[-1])
        await callback.answer()
        return await render_queue(callback.message, offset=offset, edit=True)
    if data.startswith("job:cancel:"):
        job_id = int(data.rsplit(":", 1)[-1])
        result = await asyncio.to_thread(queue.request_cancel, job_id)
        await callback.answer("Cancellation requested." if result else "Job is no longer cancellable.")
        return await render_queue(callback.message, edit=True)
    if data.startswith("job:retry:"):
        job_id = int(data.rsplit(":", 1)[-1])
        old = await asyncio.to_thread(queue.get, job_id)
        if not old or old["status"] != "failed":
            return await callback.answer("Only failed jobs can be retried.", show_alert=True)
        status = await callback.message.reply_text("🔁 Requeueing failed job…")
        job, created = await asyncio.to_thread(
            queue.enqueue, old["video_id"], old["post_url"], 720,
            requested_by=uid, status_chat_id=status.chat.id, status_message_id=status.id,
            upload_screenshots=old.get("upload_screenshots", True))
        await callback.answer("Retry queued." if created else "Job already queued.")
        await status.edit_text(f"🔁 Job #{job['id']} · {html.escape(str(job['status']))}")
        return await render_queue(callback.message, edit=True)
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
app.add_handler(MessageHandler(channel_post_peer_cache, filters.channel))
app.add_handler(MessageHandler(forwarded_destination_handler, filters.private & filters.forwarded))
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
app.add_handler(CallbackQueryHandler(callback_handler, filters.regex(r"^(home:|settings:|queue:|job:|channel:|batch:cancel:)")))
app.add_handler(MessageHandler(text_handler, filters.text & filters.private & ~filters.regex(r"^/")))


async def run_bot():
    await app.start()
    try:
        await upload_client.start()
        await screenshot_client.start()
    except Exception:
        if upload_client.is_connected:
            await upload_client.stop()
        await app.stop()
        raise
    await on_start(app)
    log.info(
        "TMJDLC started | update=tmjdlc_bot | video_upload=tmjdlc_upload "
        "| screenshot_upload=tmjdlc_screenshots | video transmissions=%s | screenshot transmissions=%s",
        UPLOAD_WORKERS, SCREENSHOT_UPLOAD_WORKERS,
    )
    try:
        await idle()
    finally:
        await on_stop(app)
        await screenshot_client.stop()
        await upload_client.stop()
        await app.stop()


def main():
    app.run(run_bot())


if __name__ == "__main__":
    main()
