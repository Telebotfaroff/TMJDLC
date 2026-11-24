"""TMJDLC admin bot using WZGram MTProto clients for high-throughput uploads."""
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
from inspect import signature

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait
from pyrogram.enums import ParseMode, ChatMemberStatus, ChatType
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from tmjdlc.bot_queue import JobQueue
from tmjdlc.providers.javtiful import JavtifulProvider
from tmjdlc.providers.javtiful.channel import JavtifulChannelScraper
from tmjdlc.gofile import (
    ensure_guest_folder, finalize_current_folder, upload_file as gofile_upload_file,
)
from tmjdlc.telegram_media import prepare_telegram_parts, TELEGRAM_MAX_FILE_BYTES

load_dotenv()
from tmjdlc.github_store import GitHubDataStore, GitHubStoreError

github_store = GitHubDataStore.from_env()
github_secrets, github_settings = github_store.load_runtime_config()
for _key, _value in github_secrets.items():
    if _value not in (None, ""):
        os.environ[str(_key)] = str(_value)
for _key, _value in github_settings.items():
    if _value is not None:
        os.environ.setdefault(str(_key), str(_value).lower() if isinstance(_value, bool) else str(_value))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
TELEGRAM_LOG_LEVEL = os.getenv("TMJDLC_TELEGRAM_LOG_LEVEL", "INFO").strip().upper()
logging.getLogger("pyrogram").setLevel(getattr(logging, TELEGRAM_LOG_LEVEL, logging.INFO))
log = logging.getLogger("tmjdlc.telegram")

TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "").strip()
ADMINS = {int(x.strip()) for x in os.getenv("TG_ADMIN_IDS", "").split(",") if x.strip()}
MAX_WORKERS = max(1, min(2, int(os.getenv("TMJDLC_MAX_PARALLEL", "2"))))
UPLOAD_TRANSMISSION_SLOTS = max(1, min(32, int(os.getenv("TMJDLC_UPLOAD_TRANSMISSION_SLOTS", os.getenv("TMJDLC_UPLOAD_WORKERS", "8")))))
HELPER_BOT_TOKENS = [token.strip() for token in re.split(r"[\s,]+", os.getenv("TG_HELPER_BOT_TOKENS", "")) if token.strip()]
SECONDARY_BOT_TOKEN = os.getenv("TG_SECONDARY_BOT_TOKEN", "").strip()
SECONDARY_API_ID = int(os.getenv("TG_SECONDARY_API_ID", "0") or 0)
SECONDARY_API_HASH = os.getenv("TG_SECONDARY_API_HASH", "").strip()
SECONDARY_CONFIGURED = bool(SECONDARY_BOT_TOKEN or SECONDARY_API_ID or SECONDARY_API_HASH)
MAX_UPLOAD_WORKERS = 4
if SECONDARY_CONFIGURED and not (SECONDARY_BOT_TOKEN and SECONDARY_API_ID and SECONDARY_API_HASH):
    raise RuntimeError(
        "Secondary uploader requires TG_SECONDARY_BOT_TOKEN, "
        "TG_SECONDARY_API_ID and TG_SECONDARY_API_HASH in config/secrets.json."
    )
UPLOAD_FLOOD_RETRY_LIMIT = max(1, min(10, int(os.getenv("TMJDLC_UPLOAD_FLOOD_RETRIES", "5"))))
DOWNLOAD_FRAGMENTS = max(1, min(16, int(os.getenv("TMJDLC_DOWNLOAD_FRAGMENTS", "8"))))
POLL_SECONDS = max(2, int(os.getenv("TMJDLC_QUEUE_POLL_SECONDS", "4")))
MAX_PENDING_UPLOADS = max(0, min(2, int(os.getenv("TMJDLC_MAX_PENDING_UPLOADS", "1"))))
UPLOAD_QUEUE_POLL_SECONDS = max(1, int(os.getenv("TMJDLC_UPLOAD_QUEUE_POLL_SECONDS", "2")))
LEASE = max(900, int(os.getenv("TMJDLC_JOB_LEASE_SECONDS", "3600")))
WORK = Path(os.getenv("TMJDLC_WORK_DIR", "/content/tmjdlc-bot"))
MIN_FREE_DISK_GB = float(os.getenv("TMJDLC_MIN_FREE_DISK_GB", "3"))
AUTO_CLEAN = os.getenv("TMJDLC_AUTO_CLEAN", "true").strip().lower() in {"1", "true", "yes", "on"}
PROGRESS_STEP = max(1, int(os.getenv("TMJDLC_NOTIFY_PROGRESS_STEP", "5")))
PROGRESS_INTERVAL_SECONDS = max(3.0, float(os.getenv("TMJDLC_NOTIFY_INTERVAL_SECONDS", "5")))
CHAT_EDIT_INTERVAL_SECONDS = max(1.0, float(os.getenv("TMJDLC_CHAT_EDIT_INTERVAL_SECONDS", "1.2")))
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
    raise RuntimeError("Add TG_BOT_TOKEN, TG_API_ID, TG_API_HASH and TG_ADMIN_IDS to config/secrets.json in the private GitHub data repository.")

queue = JobQueue(store=github_store, max_attempts=int(os.getenv("TMJDLC_JOB_MAX_ATTEMPTS", "3")))
WORK.mkdir(parents=True, exist_ok=True)
worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
workers: list[asyncio.Task] = []
parallel = MAX_WORKERS
active = 0
condition = asyncio.Condition()
# Four upload workers share one queue and one global in-process transfer cap.
video_upload_semaphore = asyncio.Semaphore(MAX_UPLOAD_WORKERS)
setup_state: dict[int, str] = {}
channel_state: dict[int, dict] = {}
download_state: set[int] = set()
upload_ready_event = asyncio.Event()
# Worker progress/completion messages can be paused independently of queue processing.
progress_notifications_enabled = True
_status_edit_locks: dict[tuple[int, int], asyncio.Lock] = {}
_chat_status_edit_locks: dict[int, asyncio.Lock] = {}
_chat_status_edit_last_sent: dict[int, float] = {}
_synced_upload_peers: set[tuple[int, int]] = set()
_upload_client_cursor = 0
_helper_upload_clients: list[Client] = []
_upload_client_loads: dict[int, int] = {}
_helper_permission_cache: dict[tuple[int, int], tuple[float, bool]] = {}
_destination_cache: dict[tuple[int, str], tuple[float, object]] = {}
_destination_warm_tasks: dict[int, asyncio.Task] = {}
_gofile_setup_lock = asyncio.Lock()
DESTINATION_CACHE_SECONDS = 600

app = Client(
    "tmjdlc_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TOKEN,
    workers=16,
    max_concurrent_transmissions=1,
    sleep_threshold=60,
)

# Use the bot token for every upload. Oversized videos are split into
# independently playable parts below TELEGRAM_MAX_FILE_BYTES before sending.
upload_client = Client(
    "tmjdlc_upload",
    api_id=API_ID,
    api_hash=API_HASH,
    no_updates=True,
    workers=4,
    max_concurrent_transmissions=UPLOAD_TRANSMISSION_SLOTS,
    sleep_threshold=60,
    bot_token=TOKEN,
)


async def _resolve_client_upload_peer(client: Client, peer_id: int):
    """Resolve a destination using that bot's own Telegram account/access hash."""
    try:
        return await client.resolve_peer(int(peer_id))
    except Exception:
        pass

    # Public @usernames let each bot acquire its own channel access hash.
    # A private channel must already be visible in that bot's own dialogs.
    raw_destination = str(await asyncio.to_thread(
        queue.get_setting, "upload_channel", ""
    ) or "").strip()
    lookup_value = int(raw_destination) if raw_destination.lstrip("-").isdigit() else raw_destination
    if lookup_value:
        try:
            chat = await client.get_chat(lookup_value)
            if int(chat.id) == int(peer_id):
                return await client.resolve_peer(int(peer_id))
        except Exception:
            log.debug(
                "Uploader could not resolve configured destination | bot_id=%s | destination=%s",
                getattr(getattr(client, "me", None), "id", None), raw_destination,
                exc_info=True,
            )

    try:
        async for dialog in client.get_dialogs():
            if int(dialog.chat.id) == int(peer_id):
                return await client.resolve_peer(int(peer_id))
    except Exception:
        log.debug("Could not inspect uploader dialogs for peer=%s", peer_id, exc_info=True)
    raise RuntimeError(
        f"Uploader bot cannot resolve Telegram destination {peer_id}. "
        "For a public channel, configure its @username and add this bot as an administrator. "
        "For a private channel, make this bot receive a channel post first."
    )


async def sync_upload_peer(peer_id: int) -> None:
    """Warm the destination separately for every bot account using its own peer hash."""
    peer_id = int(peer_id)
    clients = [upload_client] if peer_id > 0 else [upload_client, *_helper_upload_clients]
    for client in clients:
        key = (id(client), peer_id)
        if key in _synced_upload_peers:
            continue
        try:
            peer = (
                await app.resolve_peer(peer_id)
                if client is upload_client
                else await _resolve_client_upload_peer(client, peer_id)
            )
            peer_name = type(peer).__name__
            if peer_name == "InputPeerChannel":
                access_hash, peer_type = int(peer.access_hash), "channel"
            elif peer_name == "InputPeerUser":
                access_hash, peer_type = int(peer.access_hash), "user"
            elif peer_name == "InputPeerChat":
                access_hash, peer_type = 0, "group"
            else:
                raise RuntimeError(f"Unsupported Telegram peer type for upload: {peer_name}")
            await client.storage.update_peers([(peer_id, access_hash, peer_type, None)])
            _synced_upload_peers.add(key)
        except Exception:
            if client is upload_client:
                raise
            log.warning(
                "Upload client cannot resolve destination with its own account | bot_id=%s | peer=%s",
                getattr(getattr(client, "me", None), "id", None), peer_id,
                exc_info=True,
            )


async def start_helper_upload_clients() -> None:
    """Start optional upload-only bot clients; downloads and control stay on the primary bot."""
    client_configs = []
    if SECONDARY_CONFIGURED:
        client_configs.append((
            "secondary", "tmjdlc_secondary_upload",
            SECONDARY_API_ID, SECONDARY_API_HASH, SECONDARY_BOT_TOKEN,
        ))
    for index, token in enumerate(HELPER_BOT_TOKENS, start=1):
        client_configs.append((
            f"helper-{index}", f"tmjdlc_helper_{index}", API_ID, API_HASH, token,
        ))

    seen_tokens = {TOKEN}
    for label, session_name, api_id, api_hash, token in client_configs:
        if token in seen_tokens:
            log.warning("Skipping %s because its token is already configured", label)
            continue
        seen_tokens.add(token)
        client = Client(
            session_name, api_id=api_id, api_hash=api_hash,
            bot_token=token, no_updates=(label != "secondary"), workers=4,
            max_concurrent_transmissions=UPLOAD_TRANSMISSION_SLOTS, sleep_threshold=60,
        )
        if label == "secondary":
            # The secondary bot only consumes channel-post updates to learn its
            # own private-channel peer/access hash; it has no control commands.
            client.add_handler(MessageHandler(channel_post_peer_cache, filters.channel))
        try:
            await client.start()
            _helper_upload_clients.append(client)
            _upload_client_loads[id(client)] = 0
            log.info(
                "Upload-only Telegram client started | client=%s | bot_id=%s | api_id=%s",
                label, client.me.id, api_id,
            )
        except Exception:
            log.exception("Could not start %s; continuing without that uploader", label)
            try:
                if client.is_connected:
                    await client.stop()
            except Exception:
                log.debug("Upload client cleanup failed | client=%s", label, exc_info=True)


async def stop_helper_upload_clients() -> None:
    clients = list(_helper_upload_clients)
    _helper_upload_clients.clear()
    if clients:
        await asyncio.gather(*(client.stop() for client in clients), return_exceptions=True)


async def _helper_can_post(client: Client, chat_id: int) -> bool:
    """Cache helper-bot channel permissions; private-chat fallback stays on primary bot."""
    if int(chat_id) > 0:
        return False
    key = (id(client), int(chat_id))
    cached = _helper_permission_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < DESTINATION_CACHE_SECONDS:
        return cached[1]
    try:
        await _resolve_client_upload_peer(client, chat_id)
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        allowed = member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
        if getattr(member, "can_post_messages", True) is False:
            allowed = False
    except Exception as exc:
        allowed = False
        log.info("Helper bot cannot post to destination=%s bot_id=%s: %s", chat_id, client.me.id, exc)
    _helper_permission_cache[key] = (now, allowed)
    return allowed


def is_admin(message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMINS)


async def deny(message) -> bool:
    if not is_admin(message):
        await message.reply_text("This bot is private.")
        log.warning("Rejected update from user_id=%s", message.from_user.id if message.from_user else None)
        return True
    return False


async def status_edit(chat_id, message_id, text):
    if not progress_notifications_enabled or not chat_id or not message_id:
        return
    chat_id, message_id = int(chat_id), int(message_id)
    key = (chat_id, message_id)
    message_lock = _status_edit_locks.setdefault(key, asyncio.Lock())
    chat_lock = _chat_status_edit_locks.setdefault(chat_id, asyncio.Lock())
    async with message_lock:
        async with chat_lock:
            # Keep edits to separate job messages from bursting into the same chat.
            now = time.monotonic()
            wait_for = CHAT_EDIT_INTERVAL_SECONDS - (now - _chat_status_edit_last_sent.get(chat_id, 0.0))
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            _chat_status_edit_last_sent[chat_id] = time.monotonic()
            try:
                await app.edit_message_text(chat_id, message_id, text[:4000],
                                            parse_mode=ParseMode.HTML)
            except Exception as exc:
                if "MESSAGE_NOT_MODIFIED" in str(exc):
                    return
                log.warning("Could not edit status message chat_id=%s message_id=%s: %s",
                            chat_id, message_id, exc)


async def resolve_destination_cached(value: str, client: Client = app):
    """Avoid repeating Telegram get_chat/member/peer resolution for every batch job."""
    raw = str(value or "").strip()
    key = (id(client), raw)
    cached = _destination_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < DESTINATION_CACHE_SECONDS:
        return cached[1]
    chat = await resolve_destination(raw, client)
    _destination_cache[key] = (now, chat)
    return chat


async def warm_upload_destinations(job: dict) -> str:
    """Resolve the video destination and seed upload peers while the video downloads."""
    video_value = await asyncio.to_thread(queue.get_setting, "upload_channel")
    fallback_user = int(job.get("requested_by") or 0)
    try:
        if video_value:
            chat = await resolve_destination_cached(video_value)
            await sync_upload_peer(chat.id)
        elif fallback_user:
            await sync_upload_peer(fallback_user)
    except Exception as exc:
        log.info("Video destination prewarm deferred for job=%s: %s", job.get("id"), exc)
        if fallback_user:
            try:
                await sync_upload_peer(fallback_user)
            except Exception:
                log.debug("Private-chat peer prewarm deferred for job=%s", job.get("id"),
                          exc_info=True)
    return video_value or ""


async def send_video_with_slot(status_chat_id, status_message_id, **kwargs):
    """Upload with WZGram and route channel sends through the least-loaded eligible client."""
    await status_edit(
        status_chat_id, status_message_id,
        "⏳ Waiting for the video-upload slot. Download queue is applying backpressure…",
    )
    async with video_upload_semaphore:
        chat_id = int(kwargs["chat_id"])
        candidates = [upload_client]
        if chat_id < 0 and _helper_upload_clients:
            eligible = []
            for helper in _helper_upload_clients:
                if await _helper_can_post(helper, chat_id):
                    eligible.append(helper)
            candidates.extend(eligible)
        global _upload_client_cursor
        lowest_load = min(_upload_client_loads.get(id(item), 0) for item in candidates)
        least_loaded = [item for item in candidates
                        if _upload_client_loads.get(id(item), 0) == lowest_load]
        client = least_loaded[_upload_client_cursor % len(least_loaded)]
        _upload_client_cursor += 1
        _upload_client_loads[id(client)] = _upload_client_loads.get(id(client), 0) + 1
        try:
            for attempt in range(1, UPLOAD_FLOOD_RETRY_LIMIT + 1):
                try:
                    log.info("WZGram upload selected | client=%s | destination=%s | attempt=%s",
                             getattr(getattr(client, "me", None), "username", "primary"), chat_id, attempt)
                    return await client.send_video(**kwargs)
                except FloodWait as exc:
                    delay = max(1, int(getattr(exc, "value", 1)))
                    log.warning("Telegram FloodWait | client=%s | wait=%ss | upload retry=%s/%s",
                                getattr(getattr(client, "me", None), "username", "uploader"),
                                delay, attempt, UPLOAD_FLOOD_RETRY_LIMIT)
                    if attempt >= UPLOAD_FLOOD_RETRY_LIMIT:
                        raise
                    await asyncio.sleep(delay + 1)
        finally:
            _upload_client_loads[id(client)] = max(0, _upload_client_loads.get(id(client), 1) - 1)


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
    is_upload = stage == "Upload"
    icon = "📤" if is_upload else "⬇️"
    heading = "UPLOAD IN PROGRESS" if is_upload else "DOWNLOAD IN PROGRESS"
    size = _fmt_bytes(total) if total else "size unknown"
    safe_title = html.escape(title or video_id)
    safe_id = html.escape(str(video_id))
    percent = max(0, min(100, int(percent)))
    eta_label = f"{_fmt_eta(eta)} left" if eta is not None and eta >= 0 else "calculating…"
    speed_label = f"{_fmt_bytes(speed)}/s" if speed is not None and speed > 0 else "—"
    attempt_label = f"Retry {attempt - 1}" if attempt > 1 else "First attempt"

    if not is_upload:
        return (
            f"📥 <b>DOWNLOAD</b> · <b>{safe_title}</b>\n"
            f"🎬 <code>ID {safe_id}</code>\n"
            f"<code>{_progress_bar(percent)}</code>  <b>{percent}%</b>\n"
            f"💾 <b>{_fmt_bytes(done)}</b> / {size}\n"
            f"🚀 {speed_label}  ·  ⏳ {eta_label}\n"
            f"🔄 Attempt {attempt}"
        )

    return (
        f"{icon} <b>{heading}</b>\n"
        f"<b>{safe_title}</b>\n"
        f"<code>VIDEO ID  {safe_id}</code>\n"
        f"<code>{_progress_bar(percent)}</code>  <b>{percent}%</b>\n"
        f"📦 <b>{_fmt_bytes(done)}</b> / {size}\n"
        f"⚡ {speed_label}  ·  ⏳ {eta_label}\n"
        f"🔁 {attempt_label}"
    )

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
    """Delete this completed job's local media artifacts."""
    try:
        if path.exists() and path.is_file():
            path.unlink()
        for candidate in path.parent.glob(f"javtiful-{video_id}*"):
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
        log.info("Auto-cleaned completed job media video_id=%s", video_id)
        return not path.exists()
    except OSError:
        log.exception("Auto-clean failed for video_id=%s", video_id)
        return False


async def download_job(job: dict, identity: str):
    """Download and prepare one video, then hand it to the durable upload queue."""
    jid, vid, url = int(job["id"]), str(job["video_id"]), str(job["post_url"])
    chat_id, message_id = job.get("status_chat_id"), job.get("status_message_id")
    out = WORK / "downloads"
    out.mkdir(parents=True, exist_ok=True)
    lost, stop = asyncio.Event(), asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat(jid, identity, lost, stop))
    title = video_title_from_url(url, vid)
    warm_task = asyncio.create_task(warm_upload_destinations(job))
    _destination_warm_tasks[jid] = warm_task

    def check_stop() -> None:
        if lost.is_set():
            raise RuntimeError("Download worker lease was lost.")
        if queue.is_cancel_requested(jid, identity):
            raise asyncio.CancelledError("Cancellation requested.")

    if chat_id is None:
        chat_id = job.get("requested_by")
    if progress_notifications_enabled and chat_id and not message_id:
        try:
            notice = await app.send_message(int(chat_id), f"🆕 Job #{jid} · {vid} queued for download.")
            chat_id, message_id = notice.chat.id, notice.id
            await asyncio.to_thread(queue.set_status_message, jid, chat_id, message_id)
        except Exception:
            log.exception("Could not create status message for job=%s", jid)

    try:
        check_stop()
        download_stage = await asyncio.to_thread(queue.stage_done, jid, "download")
        path = Path(download_stage["path"]) if download_stage and download_stage.get("path") else None
        if not path or not path.is_file() or path.stat().st_size == 0:
            await status_edit(chat_id, message_id, f"⬇️ Downloading {vid} (attempt {job['attempts']})…")
            last_notice = {"percent": -PROGRESS_STEP, "time": 0.0}
            loop = asyncio.get_running_loop()

            def progress_hook(data):
                if queue.is_cancel_requested(jid, identity):
                    raise RuntimeError("Cancellation requested.")
                if data.get("status") != "downloading":
                    return
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                done = data.get("downloaded_bytes", 0)
                if not total:
                    return
                percent = min(100, int(done * 100 / total))
                queue.update_progress(jid, identity, "downloading", percent)
                now = time.monotonic()
                if (now - last_notice["time"] >= PROGRESS_INTERVAL_SECONDS
                        and (percent >= last_notice["percent"] + PROGRESS_STEP or percent >= 100)):
                    last_notice.update(percent=percent, time=now)
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
            await asyncio.to_thread(queue.flush_snapshot)

        check_stop()
        media_stage = await asyncio.to_thread(queue.stage_done, jid, "media_probe")
        if not media_stage or not all(media_stage.get(k) for k in ("duration", "width", "height")):
            media_info = await asyncio.to_thread(probe_video, path)
            if not await asyncio.to_thread(
                queue.complete_stage, jid, identity, "media_probe",
                {**media_info, "size": path.stat().st_size}
            ):
                raise RuntimeError("Could not persist media metadata; worker ownership was lost.")
        else:
            media_info = {
                "duration": int(media_stage.get("duration") or 0),
                "width": int(media_stage.get("width") or 0),
                "height": int(media_stage.get("height") or 0),
            }
        await asyncio.to_thread(
            queue.save_media, vid, jid, str(path), path.stat().st_size,
            float(media_info["duration"]), media_info["width"] or None,
            media_info["height"] or None,
        )
        check_stop()
        if not await asyncio.to_thread(
            queue.set_status, jid, identity, "ready",
            output_path=str(path), lease_seconds=LEASE,
        ):
            raise RuntimeError("Could not hand the downloaded video to the upload queue.")
        # Wake the uploader immediately; the shared status message moves
        # directly from download progress to upload progress without a
        # separate "ready" notification.
        upload_ready_event.set()
        log.info("Video ready for upload | job_id=%s video_id=%s", jid, vid)
    except asyncio.CancelledError:
        await asyncio.to_thread(queue.set_status, jid, identity, "cancelled",
                                error="Cancelled by administrator.", lease_seconds=LEASE)
        await status_edit(chat_id, message_id, f"🛑 Cancelled {vid}.")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:650]}"
        log.exception("Download stage failed id=%s video_id=%s", jid, vid)
        current = await asyncio.to_thread(queue.get, jid)
        if current and current.get("worker_id") == identity:
            state = "cancelled" if current.get("cancel_requested") else (
                "queued" if int(current["attempts"]) < int(current["max_attempts"]) else "failed"
            )
            await asyncio.to_thread(queue.set_status, jid, identity, state,
                                    error=error, lease_seconds=LEASE)
            await status_edit(
                chat_id, message_id,
                f"{'⚠️ Requeued' if state == 'queued' else '❌ Failed'} download {vid}: {html.escape(error)}",
            )
    finally:
        current_job = await asyncio.to_thread(queue.get, jid)
        if current_job and current_job.get("status") != "ready":
            _destination_warm_tasks.pop(jid, None)
            if not warm_task.done():
                warm_task.cancel()
        stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


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
    if progress_notifications_enabled and notify_chat and not message_id:
        try:
            notice = await app.send_message(int(notify_chat), f"🆕 Job #{jid} · {vid} queued for processing.")
            chat_id, message_id = notice.chat.id, notice.id
            await asyncio.to_thread(queue.set_status_message, jid, chat_id, message_id)
        except Exception:
            log.exception("Could not create Telegram status message for job=%s", jid)

    try:
        upload_mode = str(await asyncio.to_thread(
            queue.get_setting, "upload_destination", "telegram"
        ) or "telegram").lower()
        if upload_mode not in {"telegram", "gofile", "both"}:
            upload_mode = "telegram"
        if upload_mode == "gofile":
            unused_warm_task = _destination_warm_tasks.pop(jid, None)
            if unused_warm_task and not unused_warm_task.done():
                unused_warm_task.cancel()
        fallback_used = False
        delivery_links = []
        video_stage = None
        gofile_receipt = None

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
                        if (now - last_download_notice["time"] >= PROGRESS_INTERVAL_SECONDS
                                and (percent >= last_download_notice["percent"] + PROGRESS_STEP or percent >= 100)):
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
                          "⚡ Upload session ready. Preparing selected destination(s)…")

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

        if upload_mode in {"telegram", "both"}:
            warm_task = _destination_warm_tasks.pop(jid, None)
            warm_settings = await warm_task if warm_task else None
            video_chat_value = warm_settings or await asyncio.to_thread(
                queue.get_setting, "upload_channel"
            )
            video_destination = None
            try:
                video_destination = await resolve_destination_cached(video_chat_value)
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
            video_stage = await asyncio.to_thread(queue.stage_done, jid, "video_upload")
            fallback_used = bool(video_stage.get("fallback", False)) if video_stage else False
            if video_stage and video_stage.get("chat_id"):
                await sync_upload_peer(int(video_stage["chat_id"]))
            default_upload_chat_id = (
                video_destination.id if video_destination is not None
                else int(job.get("requested_by") or 0)
            )
            if not default_upload_chat_id:
                raise DestinationError("No usable video destination or requesting-user chat is available.")

            if not video_stage:
                check_stop()
                parts = await asyncio.to_thread(prepare_telegram_parts, path, vid)
                part_count = len(parts)
                if part_count > 1:
                    await status_edit(
                        chat_id, message_id,
                        f"✂️ Video exceeds Telegram's file limit. Split into {part_count} parts; "
                        f"uploading sequentially…",
                    )
                upload_chat_id = default_upload_chat_id
                fallback_used = video_destination is None
                part_receipts = []
                total_bytes = sum(part.stat().st_size for part in parts)
                uploaded_bytes_before = 0

                for part_index, part_path in enumerate(parts, start=1):
                    check_stop()
                    part_stage_name = f"video_upload_part_{part_index:03d}"
                    part_stage = await asyncio.to_thread(queue.stage_done, jid, part_stage_name)
                    if part_stage:
                        part_receipts.append(part_stage)
                        upload_chat_id = int(part_stage.get("chat_id") or upload_chat_id)
                        fallback_used = bool(part_stage.get("fallback", fallback_used))
                        uploaded_bytes_before += part_path.stat().st_size
                        continue

                    part_info = await asyncio.to_thread(probe_video, part_path)
                    part_title = title if part_count == 1 else f"{title} (Part {part_index}/{part_count})"
                    thumbnail = await asyncio.to_thread(
                        create_video_thumbnail, part_path, f"{vid}-part-{part_index:03d}",
                        part_info["width"], part_info["height"],
                    )
                    part_started_at = time.monotonic()
                    last_upload_notice = {
                        "percent": -PROGRESS_STEP, "time": part_started_at,
                        "bytes": 0, "speed": 0.0,
                    }
                    part_size = part_path.stat().st_size
                    if part_size > TELEGRAM_MAX_FILE_BYTES:
                        raise RuntimeError(
                            f"Split part {part_index}/{part_count} is still larger than the safe "
                            f"Telegram limit ({part_size} > {TELEGRAM_MAX_FILE_BYTES} bytes)."
                        )
                    log.info(
                        "Telegram upload started | job_id=%s video_id=%s part=%s/%s "
                        "size=%s destination=%s",
                        jid, vid, part_index, part_count, _fmt_bytes(part_size), upload_chat_id,
                    )

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
                        if (elapsed >= PROGRESS_INTERVAL_SECONDS
                                and (percent >= last_upload_notice["percent"] + PROGRESS_STEP or percent >= 100)):
                            speed = last_upload_notice["speed"]
                            eta = (total - current) / speed if speed else None
                            overall_done = uploaded_bytes_before + current
                            overall_percent = min(100, int(overall_done * 100 / max(total_bytes, 1)))
                            await asyncio.to_thread(queue.update_progress, jid, identity, "uploading", overall_percent)
                            log.info(
                                "Telegram upload progress | job_id=%s video_id=%s part=%s/%s "
                                "part_progress=%s%% overall=%s%% speed=%s/s sent=%s/%s",
                                jid, vid, part_index, part_count, percent, overall_percent,
                                _fmt_bytes(speed), _fmt_bytes(current), _fmt_bytes(total),
                            )
                            progress_label = (
                                f"{title} · Part {part_index}/{part_count}"
                                if part_count > 1 else title
                            )
                            await status_edit(chat_id, message_id, _progress_text(
                                "Upload", vid, progress_label, overall_percent,
                                overall_done, total_bytes, speed, eta, int(job["attempts"])
                            ))
                            last_upload_notice.update(percent=percent, time=now, bytes=current)

                    try:
                        await sync_upload_peer(upload_chat_id)
                        sent = await send_video_with_slot(
                            chat_id, message_id,
                            chat_id=upload_chat_id, video=str(part_path), caption=part_title,
                            duration=part_info["duration"], width=part_info["width"],
                            height=part_info["height"], thumb=str(thumbnail) if thumbnail else None,
                            supports_streaming=True, parse_mode=ParseMode.DISABLED,
                            progress=upload_progress,
                        )
                    except Exception as channel_error:
                        fallback_chat_id = int(job.get("requested_by") or 0)
                        if not fallback_chat_id or fallback_chat_id == upload_chat_id:
                            raise
                        log.exception(
                            "Telegram channel upload failed for part %s/%s of job=%s; "
                            "trying private-chat fallback user_id=%s",
                            part_index, part_count, jid, fallback_chat_id,
                        )
                        await sync_upload_peer(fallback_chat_id)
                        sent = await send_video_with_slot(
                            chat_id, message_id,
                            chat_id=fallback_chat_id, video=str(part_path), caption=part_title,
                            duration=part_info["duration"], width=part_info["width"],
                            height=part_info["height"], thumb=str(thumbnail) if thumbnail else None,
                            supports_streaming=True, parse_mode=ParseMode.DISABLED,
                            progress=upload_progress,
                        )
                        upload_chat_id = fallback_chat_id
                        fallback_used = True

                    sent_media = getattr(sent, "video", None)
                    receipt = {
                        "chat_id": sent.chat.id, "message_id": sent.id,
                        "part": part_index, "parts": part_count, "fallback": fallback_used,
                        "file_id": getattr(sent_media, "file_id", None),
                        "file_unique_id": getattr(sent_media, "file_unique_id", None),
                        "file_size": getattr(sent_media, "file_size", part_size),
                        "file_name": getattr(sent_media, "file_name", part_path.name),
                    }
                    if not await asyncio.to_thread(
                        queue.complete_stage, jid, identity, part_stage_name, receipt
                    ):
                        raise RuntimeError(
                            f"Part {part_index}/{part_count} was sent but its receipt could not be saved."
                        )
                    await asyncio.to_thread(queue.flush_snapshot)
                    part_receipts.append(receipt)
                    uploaded_bytes_before += part_size
                    elapsed = max(time.monotonic() - part_started_at, 0.001)
                    log.info(
                        "Telegram upload completed | job_id=%s video_id=%s part=%s/%s "
                        "size=%s elapsed=%ss average_speed=%s/s message_id=%s",
                        jid, vid, part_index, part_count, _fmt_bytes(part_size), int(elapsed),
                        _fmt_bytes(part_size / elapsed), sent.id,
                    )

                summary = {
                    "chat_id": upload_chat_id,
                    "message_id": part_receipts[-1]["message_id"],
                    "parts": part_receipts,
                    "part_count": part_count,
                    "fallback": fallback_used,
                }
                if not await asyncio.to_thread(
                    queue.complete_stage, jid, identity, "video_upload", summary
                ):
                    raise RuntimeError("All parts were sent, but the final upload receipt could not be saved.")
            else:
                log.info(
                    "Skipping completed video upload for job=%s parts=%s",
                    jid, video_stage.get("part_count", 1),
                )

        if upload_mode in {"gofile", "both"}:
            gofile_stage = await asyncio.to_thread(queue.stage_done, jid, "gofile_upload")
            if gofile_stage and gofile_stage.get("download_url"):
                gofile_receipt = gofile_stage
                log.info("Skipping completed GoFile upload | job_id=%s video_id=%s", jid, vid)
            else:
                check_stop()
                await status_edit(
                    chat_id, message_id,
                    f"☁️ Uploading <b>{html.escape(title)}</b> to the shared GoFile guest folder…",
                )
                gofile_receipt = await asyncio.to_thread(gofile_upload_file, queue, path)
                if not await asyncio.to_thread(
                    queue.complete_stage, jid, identity, "gofile_upload", gofile_receipt
                ):
                    raise RuntimeError("GoFile upload finished but its receipt could not be saved.")
                await asyncio.to_thread(queue.flush_snapshot)
            completed_folder = gofile_receipt.get("folder_completed")
            if completed_folder:
                await notify_gofile_folder(completed_folder, "completed")
            gofile_link = str(gofile_receipt.get("download_url") or "")
            if gofile_link:
                delivery_links.append(
                    f'☁️ GoFile: <a href="{html.escape(gofile_link, quote=True)}">Open file</a>'
                )
            folder_link = str(gofile_receipt.get("folder_url") or "")
            if folder_link:
                delivery_links.append(
                    f'📁 Shared folder: <a href="{html.escape(folder_link, quote=True)}">Open folder</a>'
                )

        check_stop()
        if video_stage is None and upload_mode in {"telegram", "both"}:
            video_stage = await asyncio.to_thread(queue.stage_done, jid, "video_upload")
        if gofile_receipt is None and upload_mode in {"gofile", "both"}:
            gofile_receipt = await asyncio.to_thread(queue.stage_done, jid, "gofile_upload")
        await asyncio.to_thread(queue.save_upload_record, {
            "job_id": jid,
            "video_id": vid,
            "source_url": url,
            "filename": path.name,
            "file_size": path.stat().st_size,
            "duration_seconds": float(media_info.get("duration") or 0),
            "width": media_info.get("width"),
            "height": media_info.get("height"),
            "upload_destination": upload_mode,
            "telegram": video_stage or {},
            "gofile": gofile_receipt or {},
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        if not await asyncio.to_thread(queue.set_status, jid, identity, "completed",
                                       output_path=str(path), lease_seconds=LEASE):
            raise RuntimeError("Could not mark job completed because worker ownership was lost.")
        links_text = "\n".join(delivery_links)
        await status_edit(chat_id, message_id,
            f"✅ <b>Job completed</b> · <code>#{jid}</code>\n"
            f"🎬 Video: <code>{html.escape(vid)}</code>\n"
            f"📦 Uploaded: <b>{_fmt_bytes(path.stat().st_size)}</b>\n"
            f"🔁 Attempts used: <b>{job['attempts']}</b>\n"
            f"{'📩 Delivered to your private bot chat (fallback).' if fallback_used else ''}"
            f"{chr(10) + links_text if links_text else ''}")
        if AUTO_CLEAN:
            cleaned = await cleanup_completed_job(path, vid)
            if cleaned:
                await asyncio.to_thread(queue.clear_media_path, vid)
                await status_edit(chat_id, message_id,
                                  f"✅ Completed {vid}.\n"
                                  "🧹 Local video files cleaned."
                                  f"{chr(10) + links_text if links_text else ''}")
            else:
                await status_edit(chat_id, message_id,
                                  f"✅ Completed {vid}.\n"
                                  "⚠️ Upload finished, but some local files could not be cleaned."
                                  f"{chr(10) + links_text if links_text else ''}")
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


async def download_worker_loop(slot: int = 1):
    """Keep multiple downloads in flight so both upload slots stay supplied."""
    identity = f"{worker_id}-download-{slot}"
    while True:
        try:
            # Settings controls the active download count; pre-created workers
            # allow the setting to change without restarting the bot.
            if slot > parallel:
                await asyncio.sleep(UPLOAD_QUEUE_POLL_SECONDS)
                continue
            if str(await asyncio.to_thread(queue.get_setting, "queue_paused", "false")).lower() in {"1", "true", "yes", "on"}:
                await asyncio.sleep(POLL_SECONDS)
                continue
            free_gb = shutil.disk_usage(WORK).free / (1024 ** 3)
            if free_gb < MIN_FREE_DISK_GB:
                log.warning("Download paused: free disk %.2f GiB is below %.2f GiB", free_gb, MIN_FREE_DISK_GB)
                await asyncio.sleep(max(POLL_SECONDS, 15))
                continue
            upload_concurrency = max(1, min(MAX_UPLOAD_WORKERS, int(await asyncio.to_thread(
                queue.get_setting, "upload_concurrency", str(MAX_UPLOAD_WORKERS)
            ) or MAX_UPLOAD_WORKERS)))
            upload_backlog = max(0, min(2, int(await asyncio.to_thread(
                queue.get_setting, "upload_backlog_per_worker", str(MAX_PENDING_UPLOADS)
            ) or MAX_PENDING_UPLOADS)))
            job = await asyncio.to_thread(
                queue.claim, identity, LEASE,
                max_downloading=parallel,
                max_uploads=upload_concurrency,
                max_pending_uploads=upload_backlog,
            )
            if job:
                log.info(
                    "Download slot claimed | slot=%s/%s | upload_capacity=%s | job_id=%s",
                    slot, parallel, upload_concurrency, job["id"],
                )
                await download_job(job, identity)
                continue
            recovered = await asyncio.to_thread(queue.recover_expired)
            if recovered:
                log.warning("Recovered expired queue jobs=%s", recovered)
                continue
            await asyncio.sleep(UPLOAD_QUEUE_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Download worker loop error | slot=%s", slot)
            await asyncio.sleep(POLL_SECONDS)


async def upload_worker_loop(slot: int = 1):
    """Drain ready videos; idle slots wait until the configured upload concurrency needs them."""
    identity = f"{worker_id}-upload-{slot}"
    while True:
        try:
            max_uploads = max(1, min(MAX_UPLOAD_WORKERS, int(await asyncio.to_thread(
                queue.get_setting, "upload_concurrency", str(MAX_UPLOAD_WORKERS)
            ) or MAX_UPLOAD_WORKERS)))
            if slot > max_uploads:
                await asyncio.sleep(UPLOAD_QUEUE_POLL_SECONDS)
                continue
            if str(await asyncio.to_thread(queue.get_setting, "queue_paused", "false")).lower() in {"1", "true", "yes", "on"}:
                await asyncio.sleep(POLL_SECONDS)
                continue
            job = await asyncio.to_thread(queue.claim_upload, identity, LEASE)
            if job:
                await process_job(job, identity)
                continue
            # When every queued/download/upload state is empty, publish the final
            # partial GoFile folder and start a fresh public folder for the next run.
            upload_mode = str(await asyncio.to_thread(
                queue.get_setting, "upload_destination", "telegram"
            ) or "telegram").lower()
            if upload_mode in {"gofile", "both"}:
                counts = await asyncio.to_thread(queue.counts)
                unfinished = sum(int(counts.get(key, 0) or 0)
                                 for key in ("queued", "downloading", "ready", "uploading"))
                if unfinished == 0:
                    partial_folder = await asyncio.to_thread(finalize_current_folder, queue)
                    if partial_folder:
                        await notify_gofile_folder(partial_folder, "partial")
            upload_ready_event.clear()
            # Recheck after clearing so a ready event cannot be lost between
            # the empty claim and the wait.
            job = await asyncio.to_thread(queue.claim_upload, identity, LEASE)
            if job:
                await process_job(job, identity)
                continue
            try:
                await asyncio.wait_for(upload_ready_event.wait(),
                                       timeout=UPLOAD_QUEUE_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Upload worker loop error")
            await asyncio.sleep(POLL_SECONDS)

DEFAULT_MENU_ITEMS = [
    {"key": "download", "action": "download", "label": "⬇️ Download Video", "enabled": True, "order": 0},
    {"key": "batch", "action": "batch", "label": "📚 Batch Download", "enabled": True, "order": 1},
    {"key": "queue", "action": "queue", "label": "📋 Queue", "enabled": True, "order": 2},
    {"key": "settings", "action": "settings", "label": "⚙️ Settings", "enabled": True, "order": 3},
    {"key": "destination", "action": "destination", "label": "☁️ Upload Destination", "enabled": True, "order": 4},
    {"key": "storage", "action": "storage", "label": "💽 Storage", "enabled": True, "order": 5},
    {"key": "worker", "action": "worker", "label": "🖥 Worker Status", "enabled": True, "order": 6},
    {"key": "processes", "action": "processes", "label": "⚡ Live Processes", "enabled": True, "order": 7},
    {"key": "catalogue", "action": "catalogue", "label": "📚 Channel Catalogue", "enabled": True, "order": 8},
]
command_edit_state: dict[int, str] = {}


def get_menu_items() -> list[dict]:
    raw = queue.get_setting("menu_items", "")
    try:
        stored = json.loads(raw) if raw else []
    except (TypeError, json.JSONDecodeError):
        stored = []
    if not isinstance(stored, list):
        stored = []
    by_key = {
        str(item.get("key")): item for item in stored
        if isinstance(item, dict) and item.get("key")
    }
    items = []
    for default in DEFAULT_MENU_ITEMS:
        item = {**default, **by_key.get(default["key"], {})}
        item["label"] = str(item.get("label") or default["label"])[:40]
        item["enabled"] = bool(item.get("enabled", True))
        item["order"] = int(item.get("order", default["order"]))
        items.append(item)
    return sorted(items, key=lambda item: item["order"])


def save_menu_items(items: list[dict]) -> None:
    ordered = []
    for index, item in enumerate(sorted(items, key=lambda value: int(value.get("order", 0)))):
        ordered.append({**item, "order": index})
    queue.set_setting("menu_items", json.dumps(ordered, ensure_ascii=False))
    github_store.write_json("config/commands.json", {"items": ordered}, "Update TMJDLC Telegram menu layout")


def build_panel_keyboard() -> InlineKeyboardMarkup:
    # The configurable menu already contains the default quick actions.
    # Do not append a second hard-coded shortcut row, or actions such as
    # Live Processes appear twice on /start and /panel.
    visible = [item for item in get_menu_items() if item["enabled"]]
    rows = [
        [
            InlineKeyboardButton(
                item["label"],
                callback_data=f"home:{item['action']}",
            )
            for item in visible[index:index + 2]
        ]
        for index in range(0, len(visible), 2)
    ]
    rows.append([InlineKeyboardButton("🎛 Customize Menu", callback_data="menu:show")])
    return InlineKeyboardMarkup(rows)


async def render_menu_settings(target, *, edit: bool = False):
    items = get_menu_items()
    rows = []
    for item in items:
        state = "🟢 On" if item["enabled"] else "⚪ Off"
        rows.append([
            InlineKeyboardButton(item["label"][:24], callback_data="menu:noop"),
            InlineKeyboardButton(state, callback_data=f"menu:toggle:{item['key']}"),
            InlineKeyboardButton("✏️ Rename", callback_data=f"menu:rename:{item['key']}"),
        ])
        rows.append([
            InlineKeyboardButton("⬆️", callback_data=f"menu:move:{item['key']}:up"),
            InlineKeyboardButton("⬇️", callback_data=f"menu:move:{item['key']}:down"),
        ])
    rows.extend([
        [InlineKeyboardButton("♻️ Reset defaults", callback_data="menu:reset")],
        [InlineKeyboardButton("🏠 Home", callback_data="home:show")],
    ])
    body = (
        "🎛 <b>Customise Telegram Menu</b>\n\n"
        "Turn buttons on/off, rename their labels, or change their order. "
        "Changes are saved to the private GitHub data repository."
    )
    markup = InlineKeyboardMarkup(rows)
    if edit:
        return await target.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)
    return await target.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


async def commands_cmd(client, message):
    if await deny(message): return
    return await render_menu_settings(message)


async def help_cmd(client, message):
    if await deny(message): return
    body = (
        "🎬 <b>TMJDLC Help</b>\n\n"
        "<b>Downloads</b>\n"
        "• /dl URL — download one Javtiful video\n"
        "• /batch URL — start a channel batch\n"
        "• /channels — scan and browse saved channels\n\n"
        "<b>Queue</b>\n"
        "• /queue — view jobs and queue controls\n"
        "• /cancel JOB_ID — cancel a job\n"
        "• /retry JOB_ID — retry a failed job\n"
        "• /processes — live download/upload status\n\n"
        "<b>Settings</b>\n"
        "• /settings — workers, upload concurrency, backlog and notifications\n"
        "• /setupload — set the Telegram upload channel\n"
        "• /setup — guided destination setup\n"
        "• /cancelsetup — cancel channel setup\n"
        "• /commands — customize menu buttons\n\n"
        "<b>System</b>\n"
        "• /panel — overview\n"
        "• /storage — available runtime storage\n"
        "• /start — open the main menu\n\n"
        "Send a Javtiful video URL directly to enqueue it. Large videos are split into Telegram-safe parts automatically."
    )
    await message.reply_text(body, parse_mode=ParseMode.HTML)


async def start_cmd(client, message):
    if await deny(message): return
    configured_video = await asyncio.to_thread(queue.get_setting, "upload_channel")
    if not configured_video:
        setup_state[message.from_user.id] = "upload_channel"
        await message.reply_text(
            "🎞 <b>First-time setup · Video upload channel</b>\n"
            "Send <code>@channelusername</code> for a public channel, or forward a recent post "
            "from your private channel. The bot will verify administrator and posting access.\n"
            "Use /cancelsetup to stop.",
            parse_mode=ParseMode.HTML,
        )
    await panel_cmd(client, message)




async def render_dashboard(target, *, edit: bool = False):
    counts, active_jobs, destination, mode, notifications, download_workers, upload_workers = await asyncio.gather(
        asyncio.to_thread(queue.counts),
        asyncio.to_thread(queue.list_active_jobs, 4),
        asyncio.to_thread(queue.get_setting, "upload_channel", ""),
        asyncio.to_thread(queue.get_setting, "upload_destination", "telegram"),
        asyncio.to_thread(queue.get_setting, "progress_notifications", "true"),
        asyncio.to_thread(queue.get_setting, "download_concurrency", str(MAX_WORKERS)),
        asyncio.to_thread(queue.get_setting, "upload_concurrency", "1"),
    )
    free = shutil.disk_usage(WORK).free / (1024 ** 3)
    active = counts.get("downloading", 0) + counts.get("ready", 0) + counts.get("uploading", 0)
    notifications_on = str(notifications).lower() in {"1", "true", "yes", "on"}
    destination_label = html.escape(str(destination)) if destination else "Not configured"
    lines = [
        "🎬 <b>TMJDLC · Control Center</b>",
        f"{'🟢' if active or counts.get('queued', 0) else '⚪'} <b>{'WORKING' if active else ('QUEUE WAITING' if counts.get('queued', 0) else 'IDLE')}</b>",
        "",
        f"⬇️ Downloads: <b>{counts.get('downloading', 0)}/{int(download_workers or MAX_WORKERS)}</b>  ·  "
        f"⬆️ Uploads: <b>{counts.get('uploading', 0)}/{int(upload_workers or 1)}</b>",
        f"🕒 Queued: <b>{counts.get('queued', 0)}</b>  ·  📥 Waiting upload: <b>{counts.get('ready', 0)}</b>",
        f"✅ Completed: <b>{counts.get('completed', 0)}</b>  ·  ❌ Failed: <b>{counts.get('failed', 0)}</b>",
        "",
        f"📤 Destination: <code>{destination_label}</code>",
        f"☁️ Delivery: <b>{html.escape(str(mode).upper())}</b>  ·  🔔 Notifications: <b>{'ON' if notifications_on else 'PAUSED'}</b>",
        f"💽 Free storage: <b>{free:.1f} GiB</b>",
    ]
    if active_jobs:
        lines.extend(["", "<b>Recent activity</b>"])
        for job in active_jobs[:3]:
            status = html.escape(str(job.get("status") or "working").upper())
            video_id = html.escape(str(job.get("video_id") or "unknown"))
            percent = int(job.get("progress_percent") or 0)
            progress = f" · <b>{percent}%</b>" if job.get("status") in {"downloading", "uploading"} else ""
            lines.append(f"• #{job['id']} <code>{video_id}</code> · {status}{progress}")
    body = "\n".join(lines)[:3900]
    markup = build_panel_keyboard()
    if edit:
        try:
            return await target.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                return target
            raise
    return await target.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


async def panel_cmd(client, message):
    if await deny(message): return
    return await render_dashboard(message)


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
            InlineKeyboardButton("Latest 100" + (" ✓" if selection == "latest:100" else ""), callback_data="channel:select:latest:100"),
        ],
        [InlineKeyboardButton("✍️ Custom pages", callback_data="channel:custom_pages")],
        [InlineKeyboardButton(f"⏭ Skip existing: {skip}", callback_data="channel:toggle:skip")],
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
        "latest:100": "Latest 100 videos",
        "custom": f"Pages {state.get('pages', [])}",
    }.get(selection, selection)
    return (
        f"📚 <b>{html.escape(info.name)}</b>\n"
        f"Videos: <b>{info.total_videos}</b> · Pages: <b>{info.total_pages}</b>\n\n"
        f"Selection: <b>{html.escape(selection_label)}</b>\n"
        f"Skip existing: <b>{'Yes' if state.get('skip_existing', True) else 'No'}</b>\n\n"
        "Choose every option below, then press Review & queue."
    )


async def show_batch_menu(message, uid: int):
    state = channel_state[uid]
    try:
        await message.edit_text(
            batch_summary(state["info"], state),
            reply_markup=batch_keyboard(uid),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        if "MESSAGE_NOT_MODIFIED" not in str(exc):
            raise


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
        await persist_channel_info(info)
        channel_state[message.from_user.id] = {
            "info": info, "selection": "new", "quality": 720,
            "skip_existing": True,
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
                ))
        if not items:
            raise RuntimeError("No video links were found in the selected pages.")
        batch_id = uuid.uuid4().hex[:12]
        live_status = await message.reply_text(
            f"⏳ <b>Batch pipeline</b> · <code>{batch_id}</code>\n"
            f"Each video will receive its own live progress notification.",
            parse_mode=ParseMode.HTML,
        )
        # Never share one Telegram message ID across batch jobs. Each job gets
        # a dedicated notification, persisted by its download/upload worker.
        added, skipped = await asyncio.to_thread(
            queue.enqueue_many, items, skip_existing=state.get("skip_existing", True),
            batch_id=batch_id,
        )
        if not added:
            await live_status.edit_text(
                f"ℹ️ <b>No new jobs</b> · <code>{batch_id}</code>\n"
                f"All {skipped} selected videos were already in the queue.",
                parse_mode=ParseMode.HTML,
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
        label = f"#{jid} · {str(job['video_id'])[:15]} · {status}"
        action = None
        if status in {"queued", "downloading", "ready", "uploading"}:
            action = InlineKeyboardButton("✖ Cancel", callback_data=f"job:cancel:ask:{jid}")
        row = [InlineKeyboardButton("🔎 " + label, callback_data=f"job:view:{jid}")]
        if action:
            row.append(action)
        rows.append(row)
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
        [InlineKeyboardButton("🗑 Clear history", callback_data="queue:clear:ask"),
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
    lines = ["📋 <b>Active Queue</b>", f"Status: <b>{'PAUSED' if paused else 'RUNNING'}</b>",
             "<i>Only queued and active tasks are shown. Finished history is hidden.</i>", ""]
    for key in ("queued", "downloading", "ready", "uploading", "completed", "failed", "cancelled"):
        lines.append(f"{key.title()}: <b>{counts.get(key, 0)}</b>")
    lines.append("")
    lines.append(f"<b>Tasks · {offset+1}–{offset+len(jobs)}</b>" if jobs else "✅ No pending or active tasks.")
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


async def render_processes(target, *, edit: bool = False):
    counts, jobs, paused, upload_concurrency, upload_backlog = await asyncio.gather(
        asyncio.to_thread(queue.counts),
        asyncio.to_thread(queue.list_active_jobs, 20),
        asyncio.to_thread(queue.get_setting, "queue_paused", "false"),
        asyncio.to_thread(queue.get_setting, "upload_concurrency", "1"),
        asyncio.to_thread(queue.get_setting, "upload_backlog_per_worker", str(MAX_PENDING_UPLOADS)),
    )
    upload_concurrency = max(1, min(MAX_UPLOAD_WORKERS, int(upload_concurrency or MAX_UPLOAD_WORKERS)))
    upload_backlog = max(0, min(2, int(upload_backlog or 0)))
    paused = str(paused).lower() in {"1", "true", "yes", "on"}
    downloading = [job for job in jobs if job.get("status") == "downloading"]
    uploading = [job for job in jobs if job.get("status") == "uploading"]
    ready = [job for job in jobs if job.get("status") == "ready"]
    free_gb = shutil.disk_usage(WORK).free / (1024 ** 3)
    lines = [
        "⚡ <b>TMJDLC  |  LIVE CONTROL CENTER</b>",
        f"{'⏸️' if paused else '🟢'} Queue status: <b>{'PAUSED' if paused else 'RUNNING'}</b>",
        "",
        "📊 <b>WORKER OVERVIEW</b>",
        f"⬇️ Downloads   <b>{len(downloading)}/{parallel}</b> active",
        f"⬆️ Uploads       <b>{len(uploading)}/{upload_concurrency}</b> active",
        f"📦 Upload buffer  <b>{len(ready)}</b> ready  ·  limit {upload_backlog}/worker",
        f"🕒 Queued downloads  <b>{counts.get('queued', 0)}</b>",
        f"💽 Available storage  <b>{free_gb:.1f} GiB</b>",
        "",
    ]

    def add_jobs(label: str, items: list[dict], kind: str):
        lines.append(f"<b>{label}</b>  <code>{len(items)}</code>")
        if not items:
            lines.append("<i>Nothing here right now</i>")
            return
        for job in items:
            title = html.escape(str(job.get("video_id") or "unknown"))
            stage = html.escape(str(job.get("current_stage") or job.get("status") or "working")).replace("_", " ").title()
            percent = max(0, min(100, int(job.get("progress_percent") or 0)))
            worker = html.escape(str(job.get("worker_id") or "unassigned").split("-")[-1])
            attempt = int(job.get("attempts") or 0)
            max_attempts = int(job.get("max_attempts") or 0)
            lines.append(f"  <b>#{job['id']}</b>  ·  <code>{title}</code>")
            if kind == "ready":
                lines.append(f"  🟡 <b>Ready for upload</b>  ·  Worker slot pending")
            else:
                bar = _progress_bar(percent, 14)
                icon = "⬇️" if kind == "download" else "⬆️"
                lines.append(f"  {icon} {stage}  ·  <b>{percent}%</b>")
                lines.append(f"  <code>{bar}</code>")
            lines.append(f"  👷 Worker <code>{worker}</code>  ·  🔁 Attempt <b>{attempt}/{max_attempts}</b>")

    add_jobs("⬇️ ACTIVE DOWNLOADS", downloading, "download")
    lines.append("")
    add_jobs("⬆️ ACTIVE UPLOADS", uploading, "upload")
    lines.append("")
    add_jobs("⏳ READY FOR UPLOAD", ready, "ready")
    body = "\n".join(lines)[:3900]
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="processes:refresh"),
         InlineKeyboardButton("📋 Queue", callback_data="home:queue")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="home:settings"),
         InlineKeyboardButton("🏠 Home", callback_data="home:show")],
    ])
    if edit:
        try:
            return await target.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                return target
            raise
    return await target.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


async def processes_cmd(client, message):
    if await deny(message): return
    return await render_processes(message)


def catalogue_keyboard(channels: list[dict], offset: int = 0) -> InlineKeyboardMarkup:
    rows = []
    page = channels[offset:offset + 10]
    for index, channel in enumerate(page, start=offset):
        name = str(channel.get("name") or channel.get("slug") or "Javtiful channel")
        rows.append([InlineKeyboardButton(
            f"🎬 {name[:38]}", callback_data=f"catalogue:open:{index}"
        )])
    navigation = []
    if offset > 0:
        navigation.append(InlineKeyboardButton(
            "⬅️ Previous", callback_data=f"catalogue:page:{max(0, offset - 10)}"
        ))
    if offset + 10 < len(channels):
        navigation.append(InlineKeyboardButton(
            "Next ➡️", callback_data=f"catalogue:page:{offset + 10}"
        ))
    if navigation:
        rows.append(navigation)
    rows.extend([
        [InlineKeyboardButton("🔎 Scan Javtiful channels", callback_data="catalogue:scan")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"catalogue:page:{offset}"),
         InlineKeyboardButton("🏠 Home", callback_data="home:show")],
    ])
    return InlineKeyboardMarkup(rows)


async def render_catalogue(target, offset: int = 0, *, edit: bool = False):
    channels = await asyncio.to_thread(github_store.list_channels)
    updated = await asyncio.to_thread(
        github_store.read_json, "catalogue/channels.json", {"channels": [], "updated_at": None}
    )
    updated_at = updated.get("updated_at") if isinstance(updated, dict) else None
    body = (
        "📚 <b>Javtiful Channel Catalogue</b>\n"
        f"Saved channels: <b>{len(channels)}</b>\n"
        f"Last catalogue update: <code>{html.escape(str(updated_at or 'Never'))}</code>\n\n"
        "Choose a saved channel to inspect its current video count and pages, "
        "or scan Javtiful for more channel URLs."
    )
    if channels:
        body += f"\n\nShowing {offset + 1}–{min(offset + 10, len(channels))}."
    markup = catalogue_keyboard(channels, offset)
    if edit:
        try:
            return await target.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                return target
            raise
    return await target.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


async def channels_cmd(client, message):
    if await deny(message): return
    return await render_catalogue(message)


async def persist_channel_info(info):
    await asyncio.to_thread(github_store.save_channel, {
        "url": info.url,
        "slug": info.slug,
        "name": info.name,
        "total_videos": info.total_videos,
        "total_pages": info.total_pages,
        "last_page_count": info.last_page_count,
        "last_scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


async def ensure_gofile_ready() -> dict[str, str]:
    """Initialize the one persistent GoFile guest account/folder exactly once per process."""
    async with _gofile_setup_lock:
        return await asyncio.to_thread(ensure_guest_folder, queue)

async def notify_gofile_folder(folder: dict, reason: str = "completed") -> None:
    """Send each finished GoFile batch-folder link to configured admin chats once."""
    folder_id = str(folder.get("folder_id") or "").strip()
    folder_url = str(folder.get("folder_url") or "").strip()
    if not folder_id or not folder_url:
        return
    setting_key = f"gofile_notified_{folder_id}"
    already_sent = await asyncio.to_thread(queue.get_setting, setting_key, "false")
    if str(already_sent).lower() in {"1", "true", "yes", "on"}:
        return
    number = html.escape(str(folder.get("folder_number") or "?"))
    count = int(folder.get("video_count") or 0)
    label = "Folder completed" if reason == "completed" else "Queue finished · partial folder"
    notice = (
        f"📁 <b>GoFile {label}</b>\n"
        f"Folder: <b>#{number}</b>\n"
        f"Videos: <b>{count}/{20}</b>\n"
        f'<a href="{html.escape(folder_url, quote=True)}">Open public folder</a>'
    )
    delivered = False
    for admin_id in ADMINS:
        try:
            await app.send_message(admin_id, notice, parse_mode=ParseMode.HTML)
            delivered = True
        except Exception:
            log.exception("Could not send GoFile folder notice to admin=%s folder=%s", admin_id, folder_id)
    if delivered:
        await asyncio.to_thread(queue.set_setting, setting_key, "true")


async def destination_cmd(client, message, *, edit=False):
    mode = str(await asyncio.to_thread(
        queue.get_setting, "upload_destination", "telegram"
    ) or "telegram").lower()
    if mode not in {"telegram", "gofile", "both"}:
        mode = "telegram"
    folder_url, folder_code, folder_number, folder_count = await asyncio.gather(
        asyncio.to_thread(queue.get_setting, "gofile_folder_url", ""),
        asyncio.to_thread(queue.get_setting, "gofile_folder_code", ""),
        asyncio.to_thread(queue.get_setting, "gofile_folder_number", "1"),
        asyncio.to_thread(queue.get_setting, "gofile_folder_count", "0"),
    )
    folder_label = (
        f'<a href="{html.escape(folder_url, quote=True)}">Open current public folder</a>'
        if folder_url else ("Created; link unavailable" if folder_code else "Not initialized yet")
    )
    video_channel = await asyncio.to_thread(queue.get_setting, "upload_channel", "Not set")
    body = (
        "☁️ <b>Upload Destination</b>\n\n"
        f"Delivery mode: <b>{html.escape(mode.upper())}</b>\n"
        f"🎞 Telegram video channel: <code>{html.escape(str(video_channel))}</code>\n"
        "Choose where completed downloads should be delivered. Use the channel button below to change the Telegram upload channel.\n\n"
        f"Current GoFile folder: {folder_label}\n"
        f"Batch: <b>#{html.escape(str(folder_number))}</b> · Videos: <b>{int(folder_count or 0)}/20</b>\n"
        "Each public folder holds up to 20 videos. Completed folder links are sent to admins."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎞 Set Telegram upload channel", callback_data="settings:video")],
        [InlineKeyboardButton("📨 Telegram" + (" ✓" if mode == "telegram" else ""),
                              callback_data="settings:destination:telegram")],
        [InlineKeyboardButton("☁️ GoFile" + (" ✓" if mode == "gofile" else ""),
                              callback_data="settings:destination:gofile")],
        [InlineKeyboardButton("📨 + ☁️ Both" + (" ✓" if mode == "both" else ""),
                              callback_data="settings:destination:both")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="home:settings"),
         InlineKeyboardButton("🏠 Home", callback_data="home:show")],
    ])
    if edit:
        return await message.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)
    return await message.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


async def settings_cmd(client, message, check_access=True):
    if check_access and await deny(message): return
    video, paused, upload_concurrency, notifications, upload_destination, upload_backlog = await asyncio.gather(
        asyncio.to_thread(queue.get_setting, "upload_channel", "Not set"),
        asyncio.to_thread(queue.get_setting, "queue_paused", "false"),
        asyncio.to_thread(queue.get_setting, "upload_concurrency", "1"),
        asyncio.to_thread(queue.get_setting, "progress_notifications", "true"),
        asyncio.to_thread(queue.get_setting, "upload_destination", "telegram"),
        asyncio.to_thread(queue.get_setting, "upload_backlog_per_worker", str(MAX_PENDING_UPLOADS)),
    )
    upload_concurrency = max(1, min(MAX_UPLOAD_WORKERS, int(upload_concurrency or MAX_UPLOAD_WORKERS)))
    upload_backlog = max(0, min(2, int(upload_backlog or 0)))
    paused = str(paused).lower() in {"1", "true", "yes", "on"}
    notifications = str(notifications).lower() in {"1", "true", "yes", "on"}
    body = (
        "⚙️ <b>Settings</b>\n\n"
        f"🎞 Video destination: <code>{html.escape(str(video))}</code>\n"
        f"☁️ Delivery mode: <b>{html.escape(str(upload_destination).upper())}</b>\n"
        "🎥 Quality: <b>720p automatic</b>\n"
        f"⚡ Download workers: <b>{parallel}/{MAX_WORKERS}</b>\n"
        f"📤 Simultaneous uploads: <b>{upload_concurrency}</b>\n"
        f"📥 Waiting uploads per worker: <b>{upload_backlog}</b>\n"
        f"📥 Queue processing: <b>{'PAUSED' if paused else 'RUNNING'}</b>\n"
        f"🔔 Download/upload notifications: <b>{'ON' if notifications else 'PAUSED'}</b>\n"
        f"⏱ Lease: <b>{LEASE}s</b>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎞 Set video destination", callback_data="settings:video")],
        [InlineKeyboardButton("☁️ Choose upload destination", callback_data="home:destination")],
        [InlineKeyboardButton("➖ Worker", callback_data="settings:workers:down"),
         InlineKeyboardButton(f"Workers {parallel}", callback_data="settings:workers:show"),
         InlineKeyboardButton("➕ Worker", callback_data="settings:workers:up")],
        [InlineKeyboardButton(f"{n} uploads" + (" ✓" if upload_concurrency == n else ""), callback_data=f"settings:uploads:{n}")
          for n in (1, 2)],
         [InlineKeyboardButton(f"{n} uploads" + (" ✓" if upload_concurrency == n else ""), callback_data=f"settings:uploads:{n}")
          for n in (3, 4)],
        [InlineKeyboardButton(f"Backlog 0" + (" ✓" if upload_backlog == 0 else ""), callback_data="settings:backlog:0"),
         InlineKeyboardButton(f"Backlog 1" + (" ✓" if upload_backlog == 1 else ""), callback_data="settings:backlog:1"),
         InlineKeyboardButton(f"Backlog 2" + (" ✓" if upload_backlog == 2 else ""), callback_data="settings:backlog:2")],
        [InlineKeyboardButton("⏸ Pause queue" if not paused else "▶️ Resume queue",
                              callback_data="queue:pause" if not paused else "queue:resume")],
        [InlineKeyboardButton("🔕 Pause notifications" if notifications else "🔔 Resume notifications",
                              callback_data="settings:notifications:off" if notifications else "settings:notifications:on")],
        [InlineKeyboardButton("🏠 Home", callback_data="home:show"),
         InlineKeyboardButton("📋 Queue manager", callback_data="home:queue")],
    ])
    await message.reply_text(body, reply_markup=markup, parse_mode=ParseMode.HTML)


async def set_channel(message, key):
    if await deny(message): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        setup_state[message.from_user.id] = key
        return await message.reply_text(
            f"Send @channelusername for a public channel, or forward a recent post from your private video upload channel here. "
            "The bot will resolve the channel, verify posting rights, and save it in the private GitHub data repository. Send CANCEL to stop."
        )
    value = parts[1].strip()
    try:
        chat = await resolve_destination(value)
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


async def cancelsetup_cmd(client, message):
    if await deny(message):
        return
    setup_state.pop(message.from_user.id, None)
    await message.reply_text("Channel setup cancelled.")


async def setupload_cmd(client, message): await set_channel(message, "upload_channel")


def forwarded_source_chat(message):
    origin = getattr(message, "forward_origin", None)
    source = getattr(origin, "chat", None) if origin is not None else None
    if source is not None:
        return source
    # Compatibility fallback for older Pyrogram/WZGram message objects.
    return getattr(message, "forward_from_chat", None)


async def forwarded_destination_handler(client, message):
    if not message.from_user or message.from_user.id not in ADMINS:
        return
    key = setup_state.get(message.from_user.id)
    if key != "upload_channel":
        return
    source = forwarded_source_chat(message)
    if source is None or getattr(source, "type", None) != ChatType.CHANNEL:
        return await message.reply_text(
            "I couldn't read the channel origin. Forward a normal post from the private channel "
            "(use Telegram Forward, not copy/paste), or send CANCEL."
        )
    try:
        chat = await app.get_chat(source.id)
        verified = await resolve_destination(str(chat.id))
    except Exception as exc:
        return await message.reply_text(
            f"❌ Could not verify forwarded channel: {html.escape(str(exc)[:450])}\n"
            "Confirm the bot is an administrator with permission to post, then forward a fresh post."
        )
    await sync_upload_peer(verified.id)
    await asyncio.to_thread(queue.set_setting, key, str(verified.id))
    setup_state.pop(message.from_user.id, None)
    await message.reply_text(
        f"✅ Video channel verified and saved: <code>{verified.id}</code>\n"
        f"Title: {html.escape(verified.title or 'Private channel')}",
        parse_mode=ParseMode.HTML,
    )


async def channel_post_peer_cache(client, message):
    # Incoming channel_post updates seed each bot account's own peer/access-hash cache.
    try:
        await client.get_chat(message.chat.id)
        await sync_upload_peer(message.chat.id)
        log.info("Channel peer cached for upload clients | channel_id=%s | title=%s",
                 message.chat.id, getattr(message.chat, "title", None))
    except Exception:
        log.exception("Could not cache peer from channel post | channel_id=%s", message.chat.id)


async def setup_cmd(client, message):
    if await deny(message): return
    setup_state[message.from_user.id] = "upload_channel"
    await message.reply_text(
        "Forward a recent post from your private video channel here. "
        "The bot must be an administrator with permission to post."
    )


async def queue_single_url(message, url: str):
    match = VIDEO_ID.search(url)
    if not match:
        return await message.reply_text("Expected a Javtiful /video/ URL.")
    status = await message.reply_text("⏳ Adding Javtiful video to the queue…")
    job, created = await asyncio.to_thread(
        queue.enqueue, match.group(1), url, 720,
        requested_by=message.from_user.id, status_chat_id=status.chat.id,
        status_message_id=status.id,
    )
    await status.edit_text(
        f"{'🕒 Added to queue' if created else '⏭ Already exists'} · <code>{match.group(1)}</code>\n"
        f"Job #{job['id']} · {html.escape(str(job['status']))} · 720p automatic",
        parse_mode=ParseMode.HTML,
    )


async def text_handler(client, message):
    if await deny(message): return
    text = (message.text or "").strip()
    uid = message.from_user.id
    editing_key = command_edit_state.pop(uid, None)
    if editing_key:
        if text.upper() == "CANCEL":
            return await message.reply_text("Menu label change cancelled.")
        label = text.strip()
        if not label or len(label) > 40 or label.startswith("/"):
            command_edit_state[uid] = editing_key
            return await message.reply_text("Use a label of 1–40 characters (not a slash command), or send CANCEL.")
        items = get_menu_items()
        if any(item["key"] != editing_key and item["label"].casefold() == label.casefold() for item in items):
            command_edit_state[uid] = editing_key
            return await message.reply_text("That label is already used by another menu button. Choose a different label.")
        for item in items:
            if item["key"] == editing_key:
                item["label"] = label
        await asyncio.to_thread(save_menu_items, items)
        await asyncio.to_thread(queue.flush_snapshot)
        return await render_menu_settings(message)
    menu = {item["label"]: item["action"] for item in get_menu_items() if item["enabled"]}
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
        if action == "processes":
            return await render_processes(message)
        if action == "catalogue":
            return await render_catalogue(message)
        if action == "destination":
            return await destination_cmd(client, message)
        if action == "worker":
            counts = await asyncio.to_thread(queue.counts)
            active_downloads = counts.get("downloading", 0)
            active_uploads = counts.get("uploading", 0)
            return await message.reply_text(
                f"🖥 Worker Status\nDownload workers: {active_downloads}/{parallel}\n"
                f"Upload workers: {active_uploads}/{MAX_UPLOAD_WORKERS}\nQueue polling: {POLL_SECONDS}s\n"
                f"Free disk: {shutil.disk_usage(WORK).free / 1024**3:.1f} GiB"
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
        value = text
        if value.startswith("@"):
            try:
                chat = await app.get_chat(value)
                value = str(chat.id)
            except Exception as exc:
                return await message.reply_text(f"Cannot resolve channel: {exc}")
        if not re.fullmatch(r"-?\d+", value) and not re.fullmatch(r"@[A-Za-z0-9_]{5,}", value):
            return await message.reply_text(
                "For private channels, forward a recent channel post to this bot. "
                "For public channels, send @username."
            )
        try:
            chat = await resolve_destination(value)
        except DestinationError as exc:
            return await message.reply_text(f"❌ {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
        value = str(chat.id)
        await asyncio.to_thread(queue.set_setting, "upload_channel", value)
        await sync_upload_peer(chat.id)
        setup_state.pop(uid, None)
        return await message.reply_text("✅ Video upload channel saved and verified in the private GitHub data repository.")
    if CHANNEL_URL.fullmatch(text):
        status = await message.reply_text("🔎 Inspecting Javtiful channel…")
        try:
            info = await asyncio.to_thread(JavtifulChannelScraper().inspect, text)
            await persist_channel_info(info)
            channel_state[uid] = {
                "info": info, "selection": "new", "quality": 720,
                "skip_existing": True,
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
    if data == "menu:show":
        await callback.answer()
        return await render_menu_settings(callback.message, edit=True)
    if data == "menu:noop":
        return await callback.answer()
    if data == "menu:reset":
        await asyncio.to_thread(save_menu_items, [dict(item) for item in DEFAULT_MENU_ITEMS])
        await asyncio.to_thread(queue.flush_snapshot)
        await callback.answer("Menu restored to defaults.")
        return await render_menu_settings(callback.message, edit=True)
    if data.startswith("menu:toggle:"):
        key = data.rsplit(":", 1)[-1]
        items = get_menu_items()
        for item in items:
            if item["key"] == key:
                item["enabled"] = not item["enabled"]
                break
        await asyncio.to_thread(save_menu_items, items)
        await asyncio.to_thread(queue.flush_snapshot)
        await callback.answer("Menu visibility updated.")
        return await render_menu_settings(callback.message, edit=True)
    if data.startswith("menu:rename:"):
        key = data.rsplit(":", 1)[-1]
        if key not in {item["key"] for item in DEFAULT_MENU_ITEMS}:
            return await callback.answer("Unknown menu item.", show_alert=True)
        command_edit_state[uid] = key
        await callback.answer("Send the new label in chat.")
        return await callback.message.reply_text("✏️ Send the new button label (1–40 characters). Send CANCEL to stop.")
    if data.startswith("menu:move:"):
        _, _, key, direction = data.split(":", 3)
        items = get_menu_items()
        index = next((i for i, item in enumerate(items) if item["key"] == key), -1)
        target = index - 1 if direction == "up" else index + 1
        if index >= 0 and 0 <= target < len(items):
            items[index], items[target] = items[target], items[index]
            for order, item in enumerate(items):
                item["order"] = order
            await asyncio.to_thread(save_menu_items, items)
            await asyncio.to_thread(queue.flush_snapshot)
        await callback.answer("Menu order updated.")
        return await render_menu_settings(callback.message, edit=True)
    if data == "home:show":
        await callback.answer()
        return await render_dashboard(callback.message, edit=True)
    if data == "home:processes":
        await callback.answer()
        return await render_processes(callback.message, edit=True)
    if data == "home:catalogue":
        await callback.answer()
        return await render_catalogue(callback.message, edit=True)
    if data.startswith("catalogue:page:"):
        offset = int(data.rsplit(":", 1)[-1])
        await callback.answer()
        return await render_catalogue(callback.message, offset, edit=True)
    if data == "catalogue:scan":
        await callback.answer("Scanning Javtiful channel listings…")
        await callback.message.edit_text("🔎 Scanning Javtiful for channel page URLs. This may take a few minutes…")
        try:
            discovered = await asyncio.to_thread(JavtifulChannelScraper().discover_channels, max_pages=100)
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            records = [{**item, "last_scanned_at": now} for item in discovered]
            if records:
                await asyncio.to_thread(github_store.save_channels, records)
            await callback.message.edit_text(
                f"✅ <b>Channel scan completed</b>\nDiscovered: <b>{len(discovered)}</b>\n"
                "The catalogue has been saved to the private GitHub data repository.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 Open catalogue", callback_data="catalogue:page:0")]]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.exception("Javtiful channel catalogue scan failed")
            await callback.message.edit_text(f"❌ Channel scan failed: {html.escape(str(exc)[:400])}")
        return
    if data.startswith("catalogue:open:"):
        index = int(data.rsplit(":", 1)[-1])
        channels = await asyncio.to_thread(github_store.list_channels)
        if index < 0 or index >= len(channels):
            return await callback.answer("Channel entry no longer exists.", show_alert=True)
        selected = channels[index]
        await callback.answer("Inspecting selected channel…")
        try:
            info = await asyncio.to_thread(JavtifulChannelScraper().inspect, selected["url"])
            await persist_channel_info(info)
            channel_state[uid] = {"info": info, "selection": "new", "quality": 720, "skip_existing": True}
            return await callback.message.edit_text(
                batch_summary(info, channel_state[uid]),
                reply_markup=batch_keyboard(uid), parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            return await callback.message.edit_text(f"Channel inspection failed: {html.escape(str(exc)[:400])}")
    if data == "processes:refresh":
        await callback.answer("Refreshing live process data…")
        return await render_processes(callback.message, edit=True)
    if data == "home:queue":
        await callback.answer()
        return await render_queue(callback.message, edit=True)
    if data == "home:settings":
        await callback.answer()
        return await settings_cmd(client, callback.message, check_access=False)
    if data == "home:destination":
        await callback.answer()
        return await destination_cmd(client, callback.message, edit=True)
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
            f"💽 <b>Storage</b>\nTotal: {usage.total/1024**3:.1f} GiB\n"
            f"Used: {usage.used/1024**3:.1f} GiB\nFree: {usage.free/1024**3:.1f} GiB\n"
            f"New downloads pause below {MIN_FREE_DISK_GB:.1f} GiB free.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home:show")]]),
            parse_mode=ParseMode.HTML)
    if data == "home:worker":
        counts = await asyncio.to_thread(queue.counts)
        active_downloads = counts.get("downloading", 0)
        active_uploads = counts.get("uploading", 0)
        await callback.answer()
        return await callback.message.edit_text(
            f"🖥 <b>Worker Status</b>\nDownload workers: {active_downloads}/{parallel}\n"
            f"Upload workers: {active_uploads}/{MAX_UPLOAD_WORKERS}\nQueue polling: {POLL_SECONDS}s\nLease: {LEASE}s",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home:show")]]),
            parse_mode=ParseMode.HTML)
    if data.startswith("settings:destination:"):
        mode = data.rsplit(":", 1)[-1]
        if mode not in {"telegram", "gofile", "both"}:
            return await callback.answer("Unknown upload destination.", show_alert=True)
        if mode in {"gofile", "both"}:
            await callback.answer("Preparing the persistent GoFile guest folder…")
            try:
                await ensure_gofile_ready()
            except Exception as exc:
                log.exception("GoFile guest setup failed")
                await callback.message.reply_text(
                    f"❌ GoFile setup failed: {html.escape(str(exc)[:300])}"
                )
                return
        else:
            await callback.answer()
        await asyncio.to_thread(queue.set_setting, "upload_destination", mode)
        return await destination_cmd(client, callback.message, edit=True)
    if data == "settings:video":
        setup_state[uid] = "upload_channel"
        await callback.answer()
        current = await asyncio.to_thread(queue.get_setting, "upload_channel", "Not set")
        return await callback.message.reply_text(
            "🎞 <b>Set Telegram video upload channel</b>\n\n"
            f"Current channel: <code>{html.escape(str(current))}</code>\n"
            "Send the public channel @username, or forward a recent post from a private channel. "
            "I will verify that the bot is an administrator with posting permission and save the channel in the private GitHub Database repository.\n"
            "Send CANCEL to stop.",
            parse_mode=ParseMode.HTML,
        )
    if data.startswith("settings:notifications:"):
        enabled = data.rsplit(":", 1)[-1] == "on"
        global progress_notifications_enabled
        progress_notifications_enabled = enabled
        await asyncio.to_thread(queue.set_setting, "progress_notifications", "true" if enabled else "false")
        await callback.answer("Download/upload notifications resumed." if enabled else "Download/upload notifications paused.")
        return await settings_cmd(client, callback.message, check_access=False)
    if data.startswith("settings:uploads:"):
        value = int(data.rsplit(":", 1)[-1])
        if value not in {1, 2, 3, 4}:
            return await callback.answer("Choose between 1 and 4 uploads.", show_alert=True)
        await asyncio.to_thread(queue.set_setting, "upload_concurrency", str(value))
        await asyncio.to_thread(queue.flush_snapshot)
        await callback.answer(f"Simultaneous uploads set to {value}.")
        return await settings_cmd(client, callback.message, check_access=False)
    if data.startswith("settings:backlog:"):
        value = int(data.rsplit(":", 1)[-1])
        if value not in {0, 1, 2}:
            return await callback.answer("Choose a backlog of 0, 1, or 2 per worker.", show_alert=True)
        await asyncio.to_thread(queue.set_setting, "upload_backlog_per_worker", str(value))
        await asyncio.to_thread(queue.flush_snapshot)
        await callback.answer(f"Upload backlog set to {value} per worker.")
        return await settings_cmd(client, callback.message, check_access=False)
    if data.startswith("settings:workers:"):
        direction = data.rsplit(":", 1)[-1]
        if direction == "up":
            parallel = min(MAX_WORKERS, parallel + 1)
        elif direction == "down":
            parallel = max(1, parallel - 1)
        await asyncio.to_thread(queue.set_setting, "download_concurrency", str(parallel))
        await asyncio.to_thread(queue.flush_snapshot)
        async with condition:
            condition.notify_all()
        await callback.answer(f"Worker concurrency: {parallel}")
        return await settings_cmd(client, callback.message, check_access=False)
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
            "🚨 <b>Cancel ALL unfinished tasks?</b>\n"
            f"Queued: <b>{counts.get('queued', 0)}</b>\n"
            f"Downloading: <b>{counts.get('downloading', 0)}</b>\n"
            f"Uploading: <b>{counts.get('uploading', 0)}</b>\n\n"
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
            "🛑 <b>Cancel all requested</b>\n"
            f"Queued cancelled: <b>{result['cancelled']}</b>\n"
            f"Active tasks stopping: <b>{result['stopping']}</b>\n"
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
            "🗑 <b>Clear finished queue history?</b>\n"
            "This removes completed jobs not linked to the media catalogue, plus all failed and cancelled jobs. "
            "Queued and active jobs are not deleted.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Yes, clear history", callback_data="queue:clear:confirm"),
                 InlineKeyboardButton("No, go back", callback_data="home:queue")]]),
            parse_mode=ParseMode.HTML)
    if data == "queue:clear:confirm":
        count = await asyncio.to_thread(queue.clear_completed_history)
        await callback.answer(f"Deleted {count} finished queue records.", show_alert=True)
        return await render_queue(callback.message, edit=True)
    if data.startswith("queue:page:"):
        offset = int(data.rsplit(":", 1)[-1])
        await callback.answer()
        return await render_queue(callback.message, offset=offset, edit=True)
    if data.startswith("job:view:"):
        job_id = int(data.rsplit(":", 1)[-1])
        job = await asyncio.to_thread(queue.get, job_id)
        if not job:
            return await callback.answer("Job not found.", show_alert=True)
        status = str(job.get("status") or "unknown")
        video_id = html.escape(str(job.get("video_id") or "unknown"))
        post_url = html.escape(str(job.get("post_url") or ""))
        stage = html.escape(str(job.get("current_stage") or "—"))
        error = html.escape(str(job.get("last_error") or "—"))[:900]
        progress = int(job.get("progress_percent") or 0)
        attempts = f"{job.get('attempts', 0)}/{job.get('max_attempts', 0)}"
        created = html.escape(str(job.get("created_at") or "—"))
        updated = html.escape(str(job.get("updated_at") or "—"))
        body = (
            f"🧾 <b>Job #{job_id}</b>\n\n"
            f"🎬 Video: <code>{video_id}</code>\n"
            f"📌 Status: <b>{html.escape(status.upper())}</b>\n"
            f"⚙️ Stage: <b>{stage}</b>\n"
            f"📊 Progress: <b>{progress}%</b>\n"
            f"🔁 Attempts: <b>{attempts}</b>\n"
            f"🕒 Created: <code>{created}</code>\n"
            f"🔄 Updated: <code>{updated}</code>\n"
            f"🔗 Source: {post_url or '—'}\n"
            f"⚠️ Last error: <code>{error}</code>"
        )[:3900]
        rows = []
        if status in {"queued", "downloading", "ready", "uploading"}:
            rows.append([InlineKeyboardButton("✖ Cancel this task", callback_data=f"job:cancel:ask:{job_id}")])
        elif status == "failed":
            rows.append([InlineKeyboardButton("🔁 Retry job", callback_data=f"job:retry:{job_id}")])
        rows.append([InlineKeyboardButton("📋 Back to queue", callback_data="home:queue"),
                     InlineKeyboardButton("🏠 Home", callback_data="home:show")])
        await callback.answer()
        return await callback.message.edit_text(
            body, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
        )
    if data.startswith("job:cancel:ask:"):
        job_id = int(data.rsplit(":", 1)[-1])
        job = await asyncio.to_thread(queue.get, job_id)
        if not job or job.get("status") not in {"queued", "downloading", "ready", "uploading"}:
            return await callback.answer("This task is no longer cancellable.", show_alert=True)
        await callback.answer()
        return await callback.message.edit_text(
            f"✖ <b>Cancel task #{job_id}?</b>\n"
            f"Video ID: <code>{html.escape(str(job.get('video_id') or 'unknown'))}</code>\n"
            f"Current status: <b>{html.escape(str(job.get('status') or '').upper())}</b>\n\n"
            "Only this task will be cancelled. Other queue items will continue.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✖ Yes, cancel this task", callback_data=f"job:cancel:confirm:{job_id}")],
                [InlineKeyboardButton("↩ Back to queue", callback_data="home:queue")],
            ]),
            parse_mode=ParseMode.HTML,
        )
    if data.startswith("job:cancel:confirm:"):
        job_id = int(data.rsplit(":", 1)[-1])
        result = await asyncio.to_thread(queue.request_cancel, job_id)
        await callback.answer("Task cancellation requested." if result else "Task is no longer cancellable.")
        return await render_queue(callback.message, edit=True)
    if data.startswith("job:retry:"):
        job_id = int(data.rsplit(":", 1)[-1])
        old = await asyncio.to_thread(queue.get, job_id)
        if not old or old["status"] != "failed":
            return await callback.answer("Only failed jobs can be retried.", show_alert=True)
        status = await callback.message.reply_text("🔁 Requeueing failed job…")
        job, created = await asyncio.to_thread(
            queue.enqueue, old["video_id"], old["post_url"], 720,
            requested_by=uid, status_chat_id=status.chat.id, status_message_id=status.id)
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
        elif value in {"latest:10", "latest:25", "latest:50", "latest:100"}:
            state["selection"] = value
        else:
            return await callback.answer("Unknown selection.", show_alert=True)
        await callback.answer("Selection updated.")
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
        status_message_id=status.id,
    )
    await status.edit_text(f"🕒 Requeued {old['video_id']} (job #{job['id']}).")


async def log_update(client, message):
    log.info("Telegram update received | user_id=%s | chat_id=%s | text=%s | admin=%s",
             message.from_user.id if message.from_user else None,
             message.chat.id if message.chat else None,
             (message.text or "")[:120],
             bool(message.from_user and message.from_user.id in ADMINS))


async def register_telegram_commands(client):
    """Publish the supported bot commands in Telegram's Menu button."""
    commands = [
        BotCommand("start", "Open the main dashboard"),
        BotCommand("help", "Show help and usage"),
        BotCommand("panel", "Open the control panel"),
        BotCommand("dl", "Download one Javtiful video"),
        BotCommand("batch", "Download videos from a channel"),
        BotCommand("queue", "Manage queued and active jobs"),
        BotCommand("processes", "View live download and upload processes"),
        BotCommand("storage", "Check available runtime storage"),
        BotCommand("channels", "Browse the saved channel catalogue"),
        BotCommand("catalogue", "Browse the saved channel catalogue"),
        BotCommand("settings", "Configure workers and upload options"),
        BotCommand("commands", "Customize the bot's inline menu"),
        BotCommand("setup", "Configure the upload destination"),
        BotCommand("setupload", "Set the Telegram upload channel"),
        BotCommand("cancelsetup", "Cancel the current setup"),
        BotCommand("cancel", "Cancel a job by its ID"),
        BotCommand("retry", "Retry a failed job by its ID"),
    ]
    await client.set_bot_commands(commands)
    log.info("Telegram command menu registered | commands=%s", len(commands))


async def on_start(client):
    await register_telegram_commands(client)
    await asyncio.to_thread(queue.initialize)
    if not await asyncio.to_thread(queue.get_setting, "download_concurrency", ""):
        await asyncio.to_thread(queue.set_setting, "download_concurrency", str(MAX_WORKERS))
    if not await asyncio.to_thread(queue.get_setting, "upload_concurrency", ""):
        configured_uploads = max(1, min(MAX_UPLOAD_WORKERS, int(os.getenv("TMJDLC_UPLOAD_CONCURRENCY", str(MAX_UPLOAD_WORKERS)))))
        await asyncio.to_thread(queue.set_setting, "upload_concurrency", str(configured_uploads))
    # One-time migration: existing installations previously capped at two upload workers.
    if await asyncio.to_thread(queue.get_setting, "upload_concurrency_v2", "false") != "true":
        await asyncio.to_thread(queue.set_setting, "upload_concurrency", str(MAX_UPLOAD_WORKERS))
        await asyncio.to_thread(queue.set_setting, "upload_concurrency_v2", "true")
        await asyncio.to_thread(queue.flush_snapshot)
    if not await asyncio.to_thread(queue.get_setting, "upload_backlog_per_worker", ""):
        await asyncio.to_thread(queue.set_setting, "upload_backlog_per_worker", str(MAX_PENDING_UPLOADS))
    if not await asyncio.to_thread(queue.get_setting, "menu_items", ""):
        menu_config = await asyncio.to_thread(github_store.read_json, "config/commands.json", {"items": []})
        if isinstance(menu_config, dict) and menu_config.get("items"):
            await asyncio.to_thread(queue.set_setting, "menu_items", json.dumps(menu_config["items"], ensure_ascii=False))
    global parallel
    parallel = max(1, min(MAX_WORKERS, int(await asyncio.to_thread(
        queue.get_setting, "download_concurrency", str(MAX_WORKERS)
    ) or MAX_WORKERS)))
    global progress_notifications_enabled
    notification_setting = await asyncio.to_thread(
        queue.get_setting, "progress_notifications", "true"
    )
    progress_notifications_enabled = str(notification_setting).lower() in {"1", "true", "yes", "on"}
    orphaned = await asyncio.to_thread(queue.recover_orphaned_jobs, 90)
    recovered = await asyncio.to_thread(queue.recover_expired)
    log.info(
        "GitHub-backed SQLite queue ready; orphaned active jobs=%s | expired jobs=%s",
        orphaned, recovered,
    )
    global workers
    workers = [
        *[
            asyncio.create_task(download_worker_loop(slot), name=f"tmjdlc-download-worker-{slot}")
            for slot in range(1, MAX_WORKERS + 1)
        ],
        *[
            asyncio.create_task(upload_worker_loop(slot), name=f"tmjdlc-upload-worker-{slot}")
            for slot in range(1, MAX_UPLOAD_WORKERS + 1)
        ],
    ]
    me = await client.get_me()
    log.info("WZGram MTProto bot started | bot_id=%s | username=@%s | admins=%s",
             me.id, me.username, sorted(ADMINS))


async def on_stop(client):
    for task in workers:
        task.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    await asyncio.to_thread(queue.flush_snapshot)
    log.info("TMJDLC workers stopped.")



app.add_handler(MessageHandler(log_update, filters.all), group=-1)
app.add_handler(MessageHandler(channel_post_peer_cache, filters.channel))
app.add_handler(MessageHandler(forwarded_destination_handler, filters.private & filters.forwarded))
app.add_handler(MessageHandler(start_cmd, filters.command("start") & filters.private))
app.add_handler(MessageHandler(help_cmd, filters.command("help") & filters.private))
app.add_handler(MessageHandler(panel_cmd, filters.command("panel") & filters.private))
app.add_handler(MessageHandler(storage_cmd, filters.command("storage") & filters.private))
app.add_handler(MessageHandler(processes_cmd, filters.command("processes") & filters.private))
app.add_handler(MessageHandler(channels_cmd, filters.command(["channels", "catalogue"]) & filters.private))
app.add_handler(MessageHandler(commands_cmd, filters.command("commands") & filters.private))
app.add_handler(MessageHandler(batch_cmd, filters.command("batch") & filters.private))
app.add_handler(MessageHandler(settings_cmd, filters.command("settings") & filters.private))
app.add_handler(MessageHandler(queue_cmd, filters.command("queue") & filters.private))
app.add_handler(MessageHandler(setup_cmd, filters.command("setup") & filters.private))
app.add_handler(MessageHandler(setupload_cmd, filters.command("setupload") & filters.private))
app.add_handler(MessageHandler(cancelsetup_cmd, filters.command("cancelsetup") & filters.private))
app.add_handler(MessageHandler(dl_cmd, filters.command("dl") & filters.private))
app.add_handler(MessageHandler(cancel_cmd, filters.command("cancel") & filters.private))
app.add_handler(MessageHandler(retry_cmd, filters.command("retry") & filters.private))
app.add_handler(CallbackQueryHandler(callback_handler, filters.regex(r"^(home:|settings:|queue:|job:|channel:|batch:cancel:|processes:|catalogue:|menu:)")))
app.add_handler(MessageHandler(text_handler, filters.text & filters.private & ~filters.regex(r"^/")))


async def run_bot():
    await app.start()
    try:
        await upload_client.start()
        _upload_client_loads[id(upload_client)] = 0
        # Initialize the shared queue before the secondary bot begins listening
        # for channel posts used to populate its own peer cache.
        await asyncio.to_thread(queue.initialize)
        await start_helper_upload_clients()
        await on_start(app)
        log.info(
            "TMJDLC started | update=tmjdlc_bot | primary_upload_auth=%s "
            "| helper_uploaders=%s | upload_workers=%s | max_concurrent_transmissions=%s",
            "bot_token",
            len(_helper_upload_clients), MAX_UPLOAD_WORKERS, UPLOAD_TRANSMISSION_SLOTS,
        )
        await idle()
    finally:
        await on_stop(app)
        await stop_helper_upload_clients()
        if upload_client.is_connected:
            await upload_client.stop()
        if app.is_connected:
            await app.stop()


def main():
    app.run(run_bot())


if __name__ == "__main__":
    main()
