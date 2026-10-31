"""TMJDLC bot using Telegram Bot API long polling for updates and media uploads."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters

from tmjdlc.bot_queue import JobQueue
from tmjdlc.providers.javtiful import JavtifulProvider
from tmjdlc.providers.javtiful.channel import JavtifulChannelScraper

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tmjdlc.polling")
TOKEN = os.getenv("TG_BOT_TOKEN", "")
ADMINS = {int(x.strip()) for x in os.getenv("TG_ADMIN_IDS", "").split(",") if x.strip()}
MAX_WORKERS = max(1, min(4, int(os.getenv("TMJDLC_MAX_PARALLEL", "2"))))
POLL_SECONDS = max(2, int(os.getenv("TMJDLC_QUEUE_POLL_SECONDS", "4")))
LEASE = max(900, int(os.getenv("TMJDLC_JOB_LEASE_SECONDS", "3600")))
WORK = Path(os.getenv("TMJDLC_WORK_DIR", "/content/tmjdlc-bot"))
SHOT_COUNT = 10
VIDEO_ID = re.compile(r"/video/(\d+)", re.I)
CHANNEL_URL = re.compile(r"https?://(?:www\.)?javtiful\.com/channel/[\w-]+", re.I)

if not TOKEN or not ADMINS:
    raise RuntimeError("Set TG_BOT_TOKEN and TG_ADMIN_IDS.")

queue = JobQueue(max_attempts=int(os.getenv("TMJDLC_JOB_MAX_ATTEMPTS", "3")))
WORK.mkdir(parents=True, exist_ok=True)
worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
workers: list[asyncio.Task] = []
parallel = MAX_WORKERS
active = 0
condition = asyncio.Condition()
setup_state: dict[int, str] = {}
channel_state: dict[int, dict] = {}


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ADMINS)


async def deny(update: Update) -> bool:
    if not is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text("This bot is private.")
        log.warning("Rejected update from non-admin user_id=%s", update.effective_user.id if update.effective_user else None)
        return True
    return False


async def status_edit(bot, chat_id, message_id, text):
    if chat_id and message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text[:4000])
        except Exception:
            log.exception("Could not edit status message chat_id=%s message_id=%s", chat_id, message_id)


async def process_job(job: dict, identity: str, bot):
    jid, vid, url = int(job["id"]), str(job["video_id"]), str(job["post_url"])
    chat_id, message_id = job.get("status_chat_id"), job.get("status_message_id")
    out = WORK / "downloads"
    out.mkdir(parents=True, exist_ok=True)
    await status_edit(bot, chat_id, message_id, f"⬇️ Downloading {vid} (attempt {job['attempts']})…")
    try:
        path = await asyncio.to_thread(lambda: JavtifulProvider(max_attempts=2).download(url, out, f"javtiful-{vid}", quality=job.get("quality"))[0])
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Downloader returned an empty file.")
        if not queue.set_status(jid, identity, "uploading", output_path=str(path)):
            raise RuntimeError("Job lease was lost before upload.")
        video_chat = await asyncio.to_thread(queue.get_setting, "upload_channel")
        image_chat = await asyncio.to_thread(queue.get_setting, "image_channel")
        if not video_chat:
            raise RuntimeError("Set video destination first with /setupload CHANNEL_ID.")
        await status_edit(bot, chat_id, message_id, f"📤 Uploading {vid} ({path.stat().st_size / 1048576:.1f} MiB)…")
        with path.open("rb") as video_file:\n            await bot.send_video(chat_id=int(video_chat), video=video_file, caption=vid, supports_streaming=True)
        shots = []
        if image_chat and job.get("upload_screenshots", True):
            shots_dir = WORK / "screenshots" / vid
            shots_dir.mkdir(parents=True, exist_ok=True)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, check=True)
            duration = float(probe.stdout.strip())
            for i in range(SHOT_COUNT):
                target = shots_dir / f"{vid}-{i+1:02d}.jpg"
                stamp = duration * (i + 1) / (SHOT_COUNT + 1)
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(stamp), "-i", str(path), "-frames:v", "1", "-q:v", "3", "-y", str(target)], capture_output=True, check=True)
                shots.append(target)
            for i, shot in enumerate(shots, 1):
                with shot.open("rb") as photo_file:\n                    await bot.send_photo(chat_id=int(image_chat), photo=photo_file, caption=f"{vid} | Screenshot {i}/{SHOT_COUNT}")
        queue.set_status(jid, identity, "completed", output_path=str(path))
        await status_edit(bot, chat_id, message_id, f"✅ Completed {vid}. Screenshots sent: {len(shots)}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:650]}"
        log.exception("Job failed id=%s video_id=%s", jid, vid)
        current = await asyncio.to_thread(queue.get, jid)
        if current and current.get("worker_id") == identity:
            state = "queued" if int(current["attempts"]) < int(current["max_attempts"]) else "failed"
            queue.set_status(jid, identity, state, error=error)
            await status_edit(bot, chat_id, message_id, f"{'⚠️ Requeued' if state == 'queued' else '❌ Failed'} {vid}: {error}")


async def worker_loop(index: int, bot):
    global active
    identity = f"{worker_id}-{index}"
    while True:
        try:
            async with condition:
                await condition.wait_for(lambda: active < parallel)
                active += 1
            try:
                job = await asyncio.to_thread(queue.claim, identity, LEASE)
                if job:
                    await process_job(job, identity, bot)
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


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update): return
    await update.effective_message.reply_text(
        "TMJDLC Durable Queue Bot (Bot API long polling)\n\n"
        "Send a Javtiful channel URL to inspect it.\n"
        "/dl POST_URL — queue one video\n/queue — queue status\n"
        "/setup — guided channel setup\n/setupload CHANNEL_ID — video destination\n"
        "/setimages CHANNEL_ID — screenshot destination\n/settings — configuration\n"
        "/cancel JOB_ID — cancel queued job\n/retry JOB_ID — retry failed job"
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update): return
    video, images = await asyncio.gather(
        asyncio.to_thread(queue.get_setting, "upload_channel", "Not set"),
        asyncio.to_thread(queue.get_setting, "image_channel", "Not set"))
    await update.effective_message.reply_text(f"Video destination: {video}\nScreenshot destination: {images}\nWorkers: {parallel}/{MAX_WORKERS}\nLease: {LEASE}s")


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update): return
    counts, jobs = await asyncio.gather(asyncio.to_thread(queue.counts), asyncio.to_thread(queue.list_jobs, 12))
    summary = "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "No jobs yet."
    recent = "\n".join(f"#{x['id']} · {x['video_id']} · {x['status']} · try {x['attempts']}" for x in jobs)
    await update.effective_message.reply_text(f"📊 Queue\n{summary}\n\nRecent jobs\n{recent}"[:4000])


async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str):
    if await deny(update): return
    if not context.args:
        return await update.effective_message.reply_text("Usage: /setupload -1001234567890")
    value = context.args[0]
    await asyncio.to_thread(queue.set_setting, key, value)
    await update.effective_message.reply_text(f"✅ Saved {value} in Neon.")


async def setupload_cmd(u, c): await set_channel(u, c, "upload_channel")
async def setimages_cmd(u, c): await set_channel(u, c, "image_channel")


async def setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update): return
    setup_state[update.effective_user.id] = "upload_channel"
    await update.effective_message.reply_text("Step 1/2: send your video channel @username or numeric ID. The bot must be an admin there.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update): return
    text = (update.effective_message.text or "").strip()
    uid = update.effective_user.id
    stage = setup_state.get(uid)
    if stage:
        if text.upper() == "CANCEL":
            setup_state.pop(uid, None)
            return await update.effective_message.reply_text("Setup cancelled.")
        if stage == "image_channel" and text.upper() == "SKIP":
            await asyncio.to_thread(queue.set_setting, "image_channel", "")
            setup_state.pop(uid, None)
            return await update.effective_message.reply_text("✅ Setup complete. Screenshot channel disabled.")
        value = text
        if value.startswith("@"):
            try:
                chat = await context.bot.get_chat(value)
                value = str(chat.id)
            except Exception as exc:
                return await update.effective_message.reply_text(f"Cannot resolve channel: {exc}")
        if not re.fullmatch(r"-?\d+", value):
            return await update.effective_message.reply_text("Send a numeric channel ID or public @username.")
        key = "upload_channel" if stage == "upload_channel" else "image_channel"
        await asyncio.to_thread(queue.set_setting, key, value)
        if stage == "upload_channel":
            setup_state[uid] = "image_channel"
            return await update.effective_message.reply_text("✅ Video channel saved. Step 2/2: send screenshot channel ID/@username, or SKIP.")
        setup_state.pop(uid, None)
        return await update.effective_message.reply_text("✅ Setup complete. Both channel IDs are saved in Neon.")
    if CHANNEL_URL.fullmatch(text):
        status = await update.effective_message.reply_text("🔎 Inspecting Javtiful channel…")
        try:
            info = await asyncio.to_thread(JavtifulChannelScraper().inspect, text)
            channel_state[uid] = {"info": info}
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Queue all pages", callback_data="channel:all"), InlineKeyboardButton("Cancel", callback_data="channel:cancel")]])
            await status.edit_text(f"📚 {info.name}\nVideos: {info.total_videos}\nPages: {info.total_pages}\nFinal page: {info.last_page_count}\nQuality: 720p", reply_markup=keyboard)
        except Exception as exc:
            await status.edit_text(f"Channel inspection failed: {type(exc).__name__}: {str(exc)[:400]}")
        return
    await update.effective_message.reply_text("Send /help to see available commands.")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMINS:
        return await query.answer("Not authorized.", show_alert=True)
    await query.answer()
    uid = query.from_user.id
    if query.data == "channel:cancel":
        channel_state.pop(uid, None)
        return await query.edit_message_text("Channel queue cancelled.")
    state = channel_state.get(uid)
    if not state:
        return await query.edit_message_text("Session expired. Send the channel URL again.")
    info = state["info"]
    await query.edit_message_text("⏳ Collecting channel links…")
    try:
        urls = await asyncio.to_thread(JavtifulChannelScraper().videos_for_pages, info, list(range(1, info.total_pages + 1)))
        items = [(VIDEO_ID.search(url).group(1), url, 720, uid, True) for url in urls]
        added, skipped = await asyncio.to_thread(queue.enqueue_many, items, skip_existing=True)
        await query.edit_message_text(f"✅ Batch queued\nVideos found: {len(urls)}\nAdded: {added}\nSkipped existing: {skipped}")
    except Exception as exc:
        await query.edit_message_text(f"❌ Queue failed: {type(exc).__name__}: {str(exc)[:400]}")
    finally:
        channel_state.pop(uid, None)


async def dl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update): return
    if not context.args:
        return await update.effective_message.reply_text("Usage: /dl https://javtiful.com/video/ID/slug")
    url = context.args[0]
    match = VIDEO_ID.search(url)
    if not match or urlparse(url).hostname not in {"javtiful.com", "www.javtiful.com"}:
        return await update.effective_message.reply_text("Expected a Javtiful video URL.")
    status = await update.effective_message.reply_text("Adding to durable queue…")
    job, created = await asyncio.to_thread(queue.enqueue, match.group(1), url, 720,
        requested_by=update.effective_user.id, status_chat_id=status.chat_id, status_message_id=status.message_id)
    await status.edit_text(f"{'🕒 Queued' if created else '⏭ Already exists'} {match.group(1)} (job #{job['id']}, {job['status']}).")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update): return
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /cancel JOB_ID")
    ok = await asyncio.to_thread(queue.cancel, int(context.args[0]))
    await update.effective_message.reply_text("🛑 Cancelled." if ok else "Job not found or already running/completed.")


async def retry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update): return
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /retry JOB_ID")
    old = await asyncio.to_thread(queue.get, int(context.args[0]))
    if not old or old["status"] != "failed":
        return await update.effective_message.reply_text("Only failed jobs can be retried.")
    status = await update.effective_message.reply_text("Requeueing…")
    job, _ = await asyncio.to_thread(queue.enqueue, old["video_id"], old["post_url"], old["quality"],
        requested_by=update.effective_user.id, status_chat_id=status.chat_id, status_message_id=status.message_id,
        upload_screenshots=old.get("upload_screenshots", True))
    await status.edit_text(f"🕒 Requeued {old['video_id']} (job #{job['id']}).")


async def log_incoming_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    log.info(
        "Telegram update received | update_id=%s | user_id=%s | chat_id=%s | text=%s | admin=%s",
        update.update_id,
        user.id if user else None,
        update.effective_chat.id if update.effective_chat else None,
        (message.text or "")[:120] if message else f"callback:{update.callback_query.data[:80]}" if update.callback_query else "non-text",
        user.id in ADMINS if user else False,
    )


async def post_init(application: Application):
    await asyncio.to_thread(queue.initialize)
    recovered = await asyncio.to_thread(queue.recover_expired)
    log.info("Neon queue ready; recovered expired jobs=%s", recovered)
    application.bot_data["worker_tasks"] = [asyncio.create_task(worker_loop(i + 1, application.bot)) for i in range(MAX_WORKERS)]
    me = await application.bot.get_me()
    log.info("Bot API polling started | bot_id=%s | username=@%s | admins=%s", me.id, me.username, sorted(ADMINS))


async def post_shutdown(application: Application):
    for task in application.bot_data.get("worker_tasks", []):
        task.cancel()
    if application.bot_data.get("worker_tasks"):
        await asyncio.gather(*application.bot_data["worker_tasks"], return_exceptions=True)


def main():
    application = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(TypeHandler(Update, log_incoming_update), group=-1)\n    application.add_handler(CommandHandler(["start", "help"], start_cmd))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CommandHandler("queue", queue_cmd))
    application.add_handler(CommandHandler("setup", setup_cmd))
    application.add_handler(CommandHandler("setupload", setupload_cmd))
    application.add_handler(CommandHandler("setimages", setimages_cmd))
    application.add_handler(CommandHandler("dl", dl_cmd))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(CommandHandler("retry", retry_cmd))
    application.add_handler(CallbackQueryHandler(callback_handler, pattern=r"^channel:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("Starting Telegram Bot API long polling for ALL Telegram operations (getUpdates + sendVideo/sendPhoto)")
    application.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES, poll_interval=1.0, timeout=30)


if __name__ == "__main__":
    main()
