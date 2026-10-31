"""Admin-only Telegram controller for the TMJDLC Javtiful pipeline.

Run with: python -m tmjdlc.bot
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message

from tmjdlc.providers.javtiful import JavtifulProvider

load_dotenv()

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("TG_ADMIN_IDS", "").split(",") if x.strip()}
WORK_DIR = Path(os.getenv("TMJDLC_WORK_DIR", "/content/tmjdlc-bot"))
DB_PATH = Path(os.getenv("TMJDLC_BOT_DB", str(WORK_DIR / "bot.sqlite3")))
MAX_PARALLEL = max(1, min(4, int(os.getenv("TMJDLC_MAX_PARALLEL", "2"))))
SCREENSHOT_COUNT = 10
POST_RE = re.compile(r"https?://(?:www\.)?javtiful\.com/video/\d+/[^\s<>()]+", re.I)
VIDEO_ID_RE = re.compile(r"/video/(\d+)", re.I)

if not API_ID or not API_HASH or not BOT_TOKEN or not ADMIN_IDS:
    raise RuntimeError("Set TG_API_ID, TG_API_HASH, TG_BOT_TOKEN and TG_ADMIN_IDS in .env")

WORK_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
semaphore = asyncio.Semaphore(MAX_PARALLEL)
app = Client("tmjdlc_admin_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS settings
        (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs
        (video_id TEXT PRIMARY KEY, post_url TEXT NOT NULL, status TEXT NOT NULL,
         output_path TEXT, updated_at INTEGER NOT NULL)""")
    conn.commit()
    return conn


def setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def video_id(url: str) -> str:
    match = VIDEO_ID_RE.search(url)
    if not match or urlparse(url).hostname not in {"javtiful.com", "www.javtiful.com"}:
        raise ValueError("Expected an original Javtiful post URL.")
    return match.group(1)


def already_done(vid: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE video_id=?", (vid,)).fetchone()
    return bool(row and row[0] == "uploaded")


def mark_job(vid: str, url: str, status: str, path: str = "") -> None:
    with db() as conn:
        conn.execute("""INSERT INTO jobs(video_id,post_url,status,output_path,updated_at)
          VALUES(?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET
          post_url=excluded.post_url,status=excluded.status,
          output_path=excluded.output_path,updated_at=excluded.updated_at""",
          (vid, url, status, path, int(time.time())))


def make_screenshots(path: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    duration = float(probe.stdout.strip())
    if duration <= 1:
        raise RuntimeError("Video duration is too short for screenshot generation.")
    outputs = []
    for index in range(SCREENSHOT_COUNT):
        timestamp = duration * (index + 1) / (SCREENSHOT_COUNT + 1)
        target = out_dir / f"{path.stem}-{index + 1:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(timestamp),
             "-i", str(path), "-frames:v", "1", "-q:v", "3", "-y", str(target)],
            capture_output=True, check=True,
        )
        outputs.append(target)
    return outputs


async def progress_edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text[:4000])
    except Exception:
        pass


async def process_post(url: str, status: Message, quality: int | None = None) -> None:
    vid = video_id(url)
    if already_done(vid):
        await progress_edit(status, f"⏭ Duplicate skipped: {vid}")
        return
    async with semaphore:
        mark_job(vid, url, "downloading")
        await progress_edit(status, f"⏳ Queued {vid}. Downloading…")
        output_dir = WORK_DIR / "downloads"
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / f"javtiful-{vid}.mp4"

        def download():
            provider = JavtifulProvider(max_attempts=2)
            return provider.download(url, output_dir, f"javtiful-{vid}", quality=quality)[0]

        try:
            path = await asyncio.to_thread(download)
            if not path.exists() or path.stat().st_size == 0:
                raise RuntimeError("Download produced an empty file.")
            await progress_edit(status, f"✅ Downloaded {vid} ({path.stat().st_size / 1024**2:.1f} MiB). Generating 10 screenshots…")
            shots_dir = WORK_DIR / "screenshots" / vid
            shots = await asyncio.to_thread(make_screenshots, path, shots_dir)

            upload_chat = setting("upload_channel")
            image_chat = setting("image_channel")
            if not upload_chat:
                raise RuntimeError("Upload channel is not configured. Use /setupload CHANNEL_ID.")
            await app.send_video(
                int(upload_chat), str(path), caption=f"{vid}",
                supports_streaming=True,
            )
            if image_chat:
                for number, shot in enumerate(shots, 1):
                    await app.send_photo(
                        int(image_chat), str(shot),
                        caption=f"{vid} | Screenshot {number}/10",
                    )
            mark_job(vid, url, "uploaded", str(path))
            await progress_edit(status, f"🎉 Completed {vid}\nVideo uploaded. Screenshots: {len(shots)}")
        except Exception as exc:
            mark_job(vid, url, "failed")
            await progress_edit(status, f"❌ {vid} failed: {type(exc).__name__}: {str(exc)[:700]}")


def admin_filter():
    return filters.user(list(ADMIN_IDS))


@app.on_message(filters.command("start") & admin_filter())
async def start_handler(_, message: Message):
    await message.reply_text(
        "TMJDLC Admin Bot\n\n"
        "/dl URL [quality] — download one post\n"
        "/channel @channel [limit] — scan channel posts and queue downloads\n"
        "/setupload CHANNEL_ID — set video destination\n"
        "/setimages CHANNEL_ID — set screenshot destination\n"
        "/settings — show configuration\n"
        "/status — show queue limits and database counts\n"
        "/help — command guide"
    )


@app.on_message(filters.command("help") & admin_filter())
async def help_handler(_, message: Message):
    await start_handler(_, message)


@app.on_message(filters.command("setupload") & admin_filter())
async def set_upload(_, message: Message):
    value = " ".join(message.command[1:]).strip()
    if not value:
        return await message.reply_text("Usage: /setupload -1001234567890")
    set_setting("upload_channel", value)
    await message.reply_text(f"✅ Video upload channel saved: {value}")


@app.on_message(filters.command("setimages") & admin_filter())
async def set_images(_, message: Message):
    value = " ".join(message.command[1:]).strip()
    if not value:
        return await message.reply_text("Usage: /setimages -1001234567890")
    set_setting("image_channel", value)
    await message.reply_text(f"✅ Screenshot channel saved: {value}")


@app.on_message(filters.command("settings") & admin_filter())
async def settings_handler(_, message: Message):
    await message.reply_text(
        f"Video channel: {setting('upload_channel', 'Not set')}\n"
        f"Image channel: {setting('image_channel', 'Not set')}\n"
        f"Parallel downloads: {MAX_PARALLEL}\nScreenshots per video: 10"
    )


@app.on_message(filters.command("status") & admin_filter())
async def status_handler(_, message: Message):
    with db() as conn:
        counts = dict(conn.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())
    await message.reply_text(
        f"Parallel slots: {MAX_PARALLEL}\n"
        + "\n".join(f"{key}: {value}" for key, value in sorted(counts.items()))
    )


@app.on_message(filters.command("dl") & admin_filter())
async def download_handler(_, message: Message):
    args = message.command[1:]
    if not args:
        return await message.reply_text("Usage: /dl https://javtiful.com/video/ID/slug [720]")
    url = args[0]
    try:
        video_id(url)
        quality = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    except ValueError as exc:
        return await message.reply_text(str(exc))
    status = await message.reply_text("⏳ Accepted.")
    asyncio.create_task(process_post(url, status, quality))


@app.on_message(filters.command("channel") & admin_filter())
async def channel_handler(_, message: Message):
    args = message.command[1:]
    if not args:
        return await message.reply_text("Usage: /channel @sourcechannel [limit]")
    source = args[0]
    limit = max(1, min(5000, int(args[1]))) if len(args) > 1 and args[1].isdigit() else 500
    status = await message.reply_text(f"🔎 Scanning {source} (up to {limit} posts)…")
    urls: list[str] = []
    try:
        async for post in app.get_chat_history(source, limit=limit):
            text = " ".join(x for x in [post.text, post.caption] if x)
            for match in POST_RE.findall(text):
                urls.append(match.rstrip(".,!?]"))
        urls = list(dict.fromkeys(urls))
        if not urls:
            return await progress_edit(status, "No Javtiful post URLs found in the scanned messages.")
        await progress_edit(status, f"Found {len(urls)} unique post URL(s). Queueing…")
        for url in urls:
            task_status = await message.reply_text(f"Queued {video_id(url)}")
            asyncio.create_task(process_post(url, task_status))
        await progress_edit(status, f"✅ Scan complete. Queued {len(urls)} unique post(s). Parallel limit: {MAX_PARALLEL}.")
    except Exception as exc:
        await progress_edit(status, f"Channel scan failed: {type(exc).__name__}: {str(exc)[:600]}")


@app.on_message(filters.private & ~filters.user(list(ADMIN_IDS)))
async def deny_non_admin(_, message: Message):
    await message.reply_text("This bot is private.")


def main() -> None:
    db().close()
    print(f"TMJDLC bot starting | parallel={MAX_PARALLEL} | admin_count={len(ADMIN_IDS)}")
    app.run()


if __name__ == "__main__":
    main()
