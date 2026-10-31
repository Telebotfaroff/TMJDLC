"""Admin-only Telegram bot for the TMJDLC durable download queue."""
from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from tmjdlc.bot_queue import JobQueue
from tmjdlc.providers.javtiful import JavtifulProvider
from tmjdlc.providers.javtiful.channel import ChannelInfo, JavtifulChannelScraper

load_dotenv()
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("TG_ADMIN_IDS", "").split(",") if x.strip()}
WORK_DIR = Path(os.getenv("TMJDLC_WORK_DIR", "/content/tmjdlc-bot"))
MAX_PARALLEL = max(1, min(4, int(os.getenv("TMJDLC_MAX_PARALLEL", "2"))))
POLL_SECONDS = max(2, int(os.getenv("TMJDLC_QUEUE_POLL_SECONDS", "4")))
LEASE_SECONDS = max(900, int(os.getenv("TMJDLC_JOB_LEASE_SECONDS", "3600")))
SCREENSHOT_COUNT = 10
POST_RE = re.compile(r"https?://(?:www\.)?javtiful\.com/video/\d+/[^\s<>()]+", re.I)
VIDEO_ID_RE = re.compile(r"/video/(\d+)", re.I)

if not API_ID or not API_HASH or not BOT_TOKEN or not ADMIN_IDS:
    raise RuntimeError("Set TG_API_ID, TG_API_HASH, TG_BOT_TOKEN and TG_ADMIN_IDS.")
queue = JobQueue(max_attempts=int(os.getenv("TMJDLC_JOB_MAX_ATTEMPTS", "3")))
WORK_DIR.mkdir(parents=True, exist_ok=True)
app = Client("tmjdlc_admin_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
worker_prefix = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
worker_tasks: list[asyncio.Task] = []
setup_states: dict[int, str] = {}
channel_sessions: dict[int, dict] = {}
active_parallelism = MAX_PARALLEL
active_jobs = 0
concurrency_condition = asyncio.Condition()


def get_video_id(url: str) -> str:
    match = VIDEO_ID_RE.search(url)
    if not match or urlparse(url).hostname not in {"javtiful.com", "www.javtiful.com"}:
        raise ValueError("Expected an original Javtiful post URL.")
    return match.group(1)


def make_screenshots(path: Path, output: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    duration = float(probe.stdout.strip())
    if duration <= 1:
        raise RuntimeError("Video duration is too short.")
    files = []
    for i in range(SCREENSHOT_COUNT):
        timestamp = duration * (i + 1) / (SCREENSHOT_COUNT + 1)
        target = output / f"{path.stem}-{i + 1:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(timestamp),
             "-i", str(path), "-frames:v", "1", "-q:v", "3", "-y", str(target)],
            capture_output=True, check=True,
        )
        files.append(target)
    return files


async def edit_status(chat_id: int | None, message_id: int | None, text: str) -> None:
    if chat_id is None or message_id is None:
        return
    try:
        await app.edit_message_text(chat_id, message_id, text[:4000])
    except Exception:
        pass


async def process_job(job: dict, identity: str) -> None:
    job_id, vid, url = int(job["id"]), str(job["video_id"]), str(job["post_url"])
    chat_id, message_id, quality = job.get("status_chat_id"), job.get("status_message_id"), job.get("quality")
    output_dir = WORK_DIR / "downloads"
    output_dir.mkdir(parents=True, exist_ok=True)
    await edit_status(chat_id, message_id, f"⬇️ Downloading {vid} (attempt {job['attempts']})…")
    try:
        def download():
            return JavtifulProvider(max_attempts=2).download(
                url, output_dir, f"javtiful-{vid}", quality=quality
            )[0]

        path = await asyncio.to_thread(download)
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("Downloader returned an empty file.")
        if not queue.set_status(job_id, identity, "uploading", output_path=str(path)):
            raise RuntimeError("Job lease was lost before upload.")
        await edit_status(chat_id, message_id, f"📤 Uploading {vid} ({path.stat().st_size / 1024**2:.1f} MiB)…")
        upload_chat = await asyncio.to_thread(queue.get_setting, "upload_channel")
        image_chat = await asyncio.to_thread(queue.get_setting, "image_channel")
        if not upload_chat:
            raise RuntimeError("Set video destination first: /setupload CHANNEL_ID")
        shots = []
        if image_chat and job.get("upload_screenshots", True):
            shots = await asyncio.to_thread(make_screenshots, path, WORK_DIR / "screenshots" / vid)
        await app.send_video(int(upload_chat), str(path), caption=vid, supports_streaming=True)
        if image_chat and job.get("upload_screenshots", True):
            for index, shot in enumerate(shots, 1):
                await app.send_photo(int(image_chat), str(shot),
                    caption=f"{vid} | Screenshot {index}/{SCREENSHOT_COUNT}")
        queue.set_status(job_id, identity, "completed", output_path=str(path))
        await edit_status(chat_id, message_id, f"✅ Completed {vid}\nVideo uploaded. Screenshots sent: {len(shots)}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:700]}"
        current = await asyncio.to_thread(queue.get, job_id)
        if current and current["worker_id"] == identity:
            if int(current["attempts"]) < int(current["max_attempts"]):
                queue.set_status(job_id, identity, "queued", error=error)
                await edit_status(chat_id, message_id, f"⚠️ {vid} failed; queued for retry. {error}")
            else:
                queue.set_status(job_id, identity, "failed", error=error)
                await edit_status(chat_id, message_id, f"❌ {vid} failed: {error}")


async def worker_loop(index: int) -> None:
    global active_jobs
    identity = f"{worker_prefix}-{index}"
    while True:
        try:
            async with concurrency_condition:
                await concurrency_condition.wait_for(lambda: active_jobs < active_parallelism)
                active_jobs += 1
            try:
                job = await asyncio.to_thread(queue.claim, identity, LEASE_SECONDS)
                if job:
                    await process_job(job, identity)
                else:
                    await asyncio.sleep(POLL_SECONDS)
            finally:
                async with concurrency_condition:
                    active_jobs = max(0, active_jobs - 1)
                    concurrency_condition.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[worker-{index}] {type(exc).__name__}: {str(exc)[:300]}", flush=True)
            await asyncio.sleep(POLL_SECONDS)


async def enqueue_url(url: str, status: Message, quality: int | None = None) -> None:
    vid = get_video_id(url)
    job, created = await asyncio.to_thread(
        queue.enqueue, vid, url, quality,
        requested_by=status.from_user.id if status.from_user else None,
        status_chat_id=status.chat.id, status_message_id=status.id,
    )
    if created:
        await status.edit_text(f"🕒 Queued {vid} (job #{job['id']}).")
    else:
        await status.edit_text(f"⏭ {vid} already exists (job #{job['id']}, status: {job['status']}).")


def admin_filter():
    return filters.user(list(ADMIN_IDS))


def parse_page_selection(value: str, total_pages: int) -> list[int]:
    pages: set[int] = set()
    for part in value.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            if not a.isdigit() or not b.isdigit() or int(a) > int(b):
                raise ValueError("Use ranges like 1-5,8,10-12.")
            pages.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            pages.add(int(part))
        else:
            raise ValueError("Use page numbers like 1-5,8,10-12.")
    if not pages or min(pages) < 1 or max(pages) > total_pages:
        raise ValueError(f"Choose pages between 1 and {total_pages}.")
    return sorted(pages)


def channel_keyboard(session: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ All pages", callback_data="ch:all"),
         InlineKeyboardButton("↔️ Page range", callback_data="ch:range")],
        [InlineKeyboardButton("🔢 Select pages", callback_data="ch:pages")],
        [InlineKeyboardButton(f"🖼 Screenshots: {'ON' if session['screenshots'] else 'OFF'}", callback_data="ch:shots")],
        [InlineKeyboardButton(f"⚡ Parallel workers: {session['parallelism']}/{MAX_PARALLEL}", callback_data="ch:parallel")],
        [InlineKeyboardButton(f"⏭ Skip existing: {'ON' if session['skip_existing'] else 'OFF'}", callback_data="ch:duplicates")],
        [InlineKeyboardButton("✅ Queue selected pages", callback_data="ch:confirm"),
         InlineKeyboardButton("✖ Cancel", callback_data="ch:cancel")],
    ])


async def send_channel_options(target, user_id: int):
    s = channel_sessions[user_id]
    info: ChannelInfo = s["info"]
    text = (f"📚 **{info.name}**\nVideos: **{info.total_videos:,}**\n"
            f"Pages: **{info.total_pages:,}** (23/page; final page: {info.last_page_count})\n"
            f"Quality: **720p**\nSelected pages: **{len(s['pages'])}**")
    if isinstance(target, Message):
        return await target.reply_text(text, reply_markup=channel_keyboard(s))
    return await target.edit_text(text, reply_markup=channel_keyboard(s))


@app.on_callback_query(filters.user(list(ADMIN_IDS)) & filters.regex(r"^ch:"))
async def channel_callback(_, query: CallbackQuery):
    global active_parallelism
    uid = query.from_user.id
    s = channel_sessions.get(uid)
    if not s:
        return await query.answer("Session expired.", show_alert=True)
    action = query.data.split(":", 1)[1]
    if action == "cancel":
        channel_sessions.pop(uid, None)
        await query.message.edit_text("Channel download cancelled.")
        return await query.answer()
    if action == "all":
        s["pages"] = list(range(1, s["info"].total_pages + 1))
    elif action in {"range", "pages"}:
        s["input_mode"] = action
        await query.answer()
        prompt = "Send a page range, e.g. 1-10." if action == "range" else "Send pages, e.g. 1,3,5-8."
        return await query.message.reply_text(prompt)
    elif action == "shots":
        s["screenshots"] = not s["screenshots"]
    elif action == "duplicates":
        s["skip_existing"] = not s["skip_existing"]
    elif action == "parallel":
        active_parallelism = 1 if active_parallelism >= MAX_PARALLEL else active_parallelism + 1
        s["parallelism"] = active_parallelism
        async with concurrency_condition:
            concurrency_condition.notify_all()
    elif action == "confirm":
        pages = s["pages"]
        if not pages:
            return await query.answer("Select pages first.", show_alert=True)
        await query.answer("Collecting video links…")
        await query.message.edit_text(f"⏳ Collecting links from {len(pages)} page(s)…")
        try:
            urls = await asyncio.to_thread(JavtifulChannelScraper().videos_for_pages, s["info"], pages)
            added = skipped = 0
            for url in urls:
                vid = get_video_id(url)
                existing = await asyncio.to_thread(queue.get_by_video, vid)
                if s["skip_existing"] and existing:
                    skipped += 1
                    continue
                _, created = await asyncio.to_thread(queue.enqueue, vid, url, 720,
                    requested_by=uid, upload_screenshots=s["screenshots"])
                if created:
                    added += 1
                else:
                    skipped += 1
            await query.message.edit_text(
                f"✅ Channel batch queued\nChannel: {s['info'].name}\nPages: {len(pages)}\n"
                f"Videos found: {len(urls)}\nAdded: {added}\nSkipped existing: {skipped}\n"
                f"Quality: 720p | Screenshots: {'ON' if s['screenshots'] else 'OFF'}\n"
                f"Worker parallelism: {active_parallelism}/{MAX_PARALLEL}"
            )
        except Exception as exc:
            await query.message.edit_text(f"❌ Channel queue failed: {type(exc).__name__}: {str(exc)[:450]}")
        finally:
            channel_sessions.pop(uid, None)
        return
    await query.answer()
    info = s["info"]
    await query.message.edit_text(
        f"📚 **{info.name}**\nVideos: **{info.total_videos:,}**\n"
        f"Pages: **{info.total_pages:,}** (23/page; final page: {info.last_page_count})\n"
        f"Quality: **720p**\nSelected pages: **{len(s['pages'])}**",
        reply_markup=channel_keyboard(s),
    )


@app.on_message(filters.command(["start", "help"]) & admin_filter())
async def help_handler(_, message: Message):
    await message.reply_text(
        "TMJDLC Durable Queue Bot\n\n"
        "Send a Javtiful /channel/SLUG URL for batch download.\n/dl URL — enqueue one 720p post\n"
        "/queue — show counts and recent jobs\n"
        "/cancel JOB_ID — cancel a queued job\n"
        "/retry JOB_ID — retry a failed job\n"
        "/setup — guided upload-channel setup\n"
        "/setupload CHANNEL_ID — video destination\n"
        "/setimages CHANNEL_ID — screenshot destination\n"
        "/settings — show configuration"
    )


@app.on_message(filters.command("setup") & admin_filter())
async def setup_handler(_, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    setup_states[user_id] = "upload_channel"
    await message.reply_text(
        "🛠 Channel setup\n\n"
        "Step 1/2 — Video upload channel\n"
        "Forward any post from your VIDEO channel here, or send its public @username / numeric channel ID.\n\n"
        "The bot must be an administrator in the destination channel."
    )


@app.on_message(filters.command("cancelsetup") & admin_filter())
async def cancel_setup_handler(_, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    setup_states.pop(user_id, None)
    await message.reply_text("Channel setup cancelled.")


@app.on_message(filters.private & filters.user(list(ADMIN_IDS)) & ~filters.command(
    ["start", "help", "setup", "cancelsetup", "setupload", "setimages", "settings",
     "queue", "cancel", "retry", "dl"]
))
async def setup_input_handler(_, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    stage = setup_states.get(user_id)
    raw_value = (message.text or message.caption or "").strip()
    session = channel_sessions.get(user_id)
    if session and session.get("input_mode"):
        mode = session.pop("input_mode")
        try:
            session["pages"] = parse_page_selection(raw_value, session["info"].total_pages)
        except ValueError as exc:
            session["input_mode"] = mode
            return await message.reply_text(str(exc))
        return await send_channel_options(message, user_id)
    if not stage and re.match(r"https?://(?:www[.])?javtiful[.]com/channel/", raw_value, re.I):
        status = await message.reply_text("🔎 Inspecting channel and pagination…")
        try:
            info = await asyncio.to_thread(JavtifulChannelScraper().inspect, raw_value)
        except Exception as exc:
            return await status.edit_text(f"Couldn't inspect channel: {type(exc).__name__}: {str(exc)[:400]}")
        channel_sessions[user_id] = {"info": info, "pages": [], "screenshots": True,
            "skip_existing": True, "parallelism": active_parallelism}
        return await send_channel_options(status, user_id)
    if not stage:
        return
    if raw_value.upper() == "CANCEL":
        setup_states.pop(user_id, None)
        return await message.reply_text("Channel setup cancelled.")

    channel_id = None
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if forwarded_chat:
        channel_id = forwarded_chat.id
    elif raw_value.startswith("@"):
        try:
            chat = await app.get_chat(raw_value)
            channel_id = chat.id
        except Exception as exc:
            return await message.reply_text(
                f"Couldn't resolve {raw_value}: {type(exc).__name__}. Try forwarding a channel post or send its numeric ID."
            )
    elif raw_value.lstrip("-").isdigit():
        channel_id = int(raw_value)

    if stage == "upload_channel":
        if channel_id is None:
            return await message.reply_text(
                "I couldn't detect a channel. Forward a post from the video channel, send @username, or send its numeric ID."
            )
        await asyncio.to_thread(queue.set_setting, "upload_channel", str(channel_id))
        setup_states[user_id] = "image_channel"
        return await message.reply_text(
            f"✅ Video channel saved: {channel_id}\n\n"
            "Step 2/2 — Screenshot image channel\n"
            "Forward any post from your IMAGE channel, or send its @username / numeric ID.\n"
            "Send SKIP if you want screenshots disabled, or CANCEL to stop."
        )

    if stage == "image_channel":
        if raw_value.upper() == "SKIP":
            await asyncio.to_thread(queue.set_setting, "image_channel", "")
            setup_states.pop(user_id, None)
            return await message.reply_text(
                "✅ Setup complete!\nVideo channel saved. Screenshot channel disabled."
            )
        if channel_id is None:
            return await message.reply_text(
                "I couldn't detect a channel. Forward a post, send @username / numeric ID, or SKIP."
            )
        await asyncio.to_thread(queue.set_setting, "image_channel", str(channel_id))
        setup_states.pop(user_id, None)
        return await message.reply_text(
            f"✅ Setup complete!\nVideo channel: {await asyncio.to_thread(queue.get_setting, 'upload_channel')}\n"
            f"Screenshot channel: {channel_id}\n\nBoth channel IDs are saved in Neon."
        )


@app.on_message(filters.command("setupload") & admin_filter())
async def set_upload(_, message: Message):
    value = " ".join(message.command[1:]).strip()
    if not value:
        return await message.reply_text("Usage: /setupload -1001234567890")
    await asyncio.to_thread(queue.set_setting, "upload_channel", value)
    await message.reply_text(f"✅ Video destination saved: {value}")


@app.on_message(filters.command("setimages") & admin_filter())
async def set_images(_, message: Message):
    value = " ".join(message.command[1:]).strip()
    if not value:
        return await message.reply_text("Usage: /setimages -1001234567890")
    await asyncio.to_thread(queue.set_setting, "image_channel", value)
    await message.reply_text(f"✅ Screenshot destination saved: {value}")


@app.on_message(filters.command("settings") & admin_filter())
async def settings_handler(_, message: Message):
    upload, images = await asyncio.gather(
        asyncio.to_thread(queue.get_setting, "upload_channel", "Not set"),
        asyncio.to_thread(queue.get_setting, "image_channel", "Not set"),
    )
    await message.reply_text(
        f"Video destination: {upload}\nScreenshot destination: {images}\n"
        f"Workers: {MAX_PARALLEL}\nLease: {LEASE_SECONDS}s\nScreenshots/video: {SCREENSHOT_COUNT}"
    )


@app.on_message(filters.command("queue") & admin_filter())
async def queue_handler(_, message: Message):
    counts, jobs = await asyncio.gather(
        asyncio.to_thread(queue.counts), asyncio.to_thread(queue.list_jobs, 12)
    )
    summary = "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "No jobs yet."
    recent = "\n".join(
        f"#{j['id']} · {j['video_id']} · {j['status']} · try {j['attempts']}" for j in jobs
    )
    await message.reply_text(f"📊 Queue\n{summary}\n\nRecent jobs\n{recent}"[:4000])


@app.on_message(filters.command("cancel") & admin_filter())
async def cancel_handler(_, message: Message):
    if len(message.command) < 2 or not message.command[1].isdigit():
        return await message.reply_text("Usage: /cancel JOB_ID")
    ok = await asyncio.to_thread(queue.cancel, int(message.command[1]))
    await message.reply_text("🛑 Cancelled." if ok else "Job not found or already running/completed.")


@app.on_message(filters.command("retry") & admin_filter())
async def retry_handler(_, message: Message):
    if len(message.command) < 2 or not message.command[1].isdigit():
        return await message.reply_text("Usage: /retry JOB_ID")
    job = await asyncio.to_thread(queue.get, int(message.command[1]))
    if not job or job["status"] != "failed":
        return await message.reply_text("Only failed jobs can be retried.")
    status = await message.reply_text("Requeueing…")
    await asyncio.to_thread(
        queue.enqueue, job["video_id"], job["post_url"], job["quality"],
        requested_by=message.from_user.id if message.from_user else None,
        status_chat_id=status.chat.id, status_message_id=status.id,
    )
    await status.edit_text(f"🕒 Requeued {job['video_id']} (job #{job['id']}).")


@app.on_message(filters.command("dl") & admin_filter())
async def download_handler(_, message: Message):
    args = message.command[1:]
    if not args:
        return await message.reply_text("Usage: /dl https://javtiful.com/video/ID/slug [720]")
    try:
        get_video_id(args[0])
        quality = 720
    except ValueError as exc:
        return await message.reply_text(str(exc))
    await enqueue_url(args[0], await message.reply_text("Adding to durable queue…"), quality)


@app.on_message(filters.private & ~filters.user(list(ADMIN_IDS)))
async def deny_non_admin(_, message: Message):
    await message.reply_text("This bot is private.")


async def startup() -> None:
    await asyncio.to_thread(queue.initialize)
    recovered = await asyncio.to_thread(queue.recover_expired)
    print(f"Queue ready; recovered expired jobs: {recovered}", flush=True)
    worker_tasks.extend(asyncio.create_task(worker_loop(i + 1)) for i in range(MAX_PARALLEL))


async def shutdown() -> None:
    for task in worker_tasks:
        task.cancel()
    if worker_tasks:
        await asyncio.gather(*worker_tasks, return_exceptions=True)


def main() -> None:
    async def runner():
        await app.start()
        try:
            await startup()
            print(f"TMJDLC bot online | workers={MAX_PARALLEL}", flush=True)
            await asyncio.Event().wait()
        finally:
            await shutdown()
            await app.stop()
    asyncio.run(runner())


if __name__ == "__main__":
    main()
