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
from pyrogram.types import Message

from tmjdlc.bot_queue import JobQueue
from tmjdlc.providers.javtiful import JavtifulProvider

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
        shots = await asyncio.to_thread(
            make_screenshots, path, WORK_DIR / "screenshots" / vid
        )
        await app.send_video(int(upload_chat), str(path), caption=vid, supports_streaming=True)
        if image_chat:
            for index, shot in enumerate(shots, 1):
                await app.send_photo(
                    int(image_chat), str(shot),
                    caption=f"{vid} | Screenshot {index}/{SCREENSHOT_COUNT}",
                )
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
    identity = f"{worker_prefix}-{index}"
    while True:
        try:
            job = await asyncio.to_thread(queue.claim, identity, LEASE_SECONDS)
            if job:
                await process_job(job, identity)
            else:
                await asyncio.sleep(POLL_SECONDS)
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


@app.on_message(filters.command(["start", "help"]) & admin_filter())
async def help_handler(_, message: Message):
    await message.reply_text(
        "TMJDLC Durable Queue Bot\n\n"
        "/dl URL [quality] — enqueue a post\n"
        "/channel @source [limit] — scan and enqueue channel posts\n"
        "/queue — show counts and recent jobs\n"
        "/cancel JOB_ID — cancel a queued job\n"
        "/retry JOB_ID — retry a failed job\n"
        "/setupload CHANNEL_ID — video destination\n"
        "/setimages CHANNEL_ID — screenshot destination\n"
        "/settings — show configuration"
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
        quality = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    except ValueError as exc:
        return await message.reply_text(str(exc))
    await enqueue_url(args[0], await message.reply_text("Adding to durable queue…"), quality)


@app.on_message(filters.command("channel") & admin_filter())
async def channel_handler(_, message: Message):
    args = message.command[1:]
    if not args:
        return await message.reply_text("Usage: /channel @sourcechannel [limit]")
    try:
        limit = max(1, min(5000, int(args[1]))) if len(args) > 1 else 500
    except ValueError:
        return await message.reply_text("Limit must be a number.")
    status = await message.reply_text(f"🔎 Scanning {args[0]}…")
    found: dict[str, str] = {}
    try:
        async for post in app.get_chat_history(args[0], limit=limit):
            caption = " ".join(x for x in (post.text, post.caption) if x)
            for match in POST_RE.findall(caption):
                clean = match.rstrip(".,!?]")
                try:
                    found[get_video_id(clean)] = clean
                except ValueError:
                    continue
        added = duplicates = 0
        for vid, url in found.items():
            job, created = await asyncio.to_thread(
                queue.enqueue, vid, url, None,
                requested_by=message.from_user.id if message.from_user else None,
                status_chat_id=status.chat.id, status_message_id=status.id,
            )
            added += int(created)
            duplicates += int(not created)
        await status.edit_text(f"✅ Scan complete\nFound: {len(found)}\nQueued: {added}\nAlready known: {duplicates}")
    except Exception as exc:
        await status.edit_text(f"Channel scan failed: {type(exc).__name__}: {str(exc)[:600]}")


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
