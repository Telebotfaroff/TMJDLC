"""TMJDLC admin bot: Pyrogram handles every Telegram update and media upload."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
POLL_SECONDS = max(2, int(os.getenv("TMJDLC_QUEUE_POLL_SECONDS", "4")))
LEASE = max(900, int(os.getenv("TMJDLC_JOB_LEASE_SECONDS", "3600")))
WORK = Path(os.getenv("TMJDLC_WORK_DIR", "/content/tmjdlc-bot"))
SHOT_COUNT = 10
MIN_FREE_DISK_GB = float(os.getenv("TMJDLC_MIN_FREE_DISK_GB", "3"))
VIDEO_ID = re.compile(r"/video/(\d+)", re.I)
CHANNEL_URL = re.compile(r"https?://(?:www\.)?javtiful\.com/channel/[\w-]+", re.I)

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

app = Client(
    "tmjdlc_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TOKEN,
    workers=16,
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
            await app.edit_message_text(chat_id, message_id, text[:4000])
        except Exception:
            log.exception("Could not edit status message chat_id=%s message_id=%s", chat_id, message_id)


async def _cancelled(job_id: int, identity: str) -> bool:
    return await asyncio.to_thread(queue.is_cancel_requested, job_id, identity)


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

    try:
        download_stage = await asyncio.to_thread(queue.stage_done, jid, "download")
        path = Path(download_stage["path"]) if download_stage and download_stage.get("path") else None
        if not path or not path.is_file() or path.stat().st_size == 0:
            check_stop()
            await status_edit(chat_id, message_id, f"⬇️ Downloading {vid} (attempt {job['attempts']})…")

            def progress_hook(data):
                if queue.is_cancel_requested(jid, identity):
                    raise RuntimeError("Cancellation requested.")
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or data.get("total_bytes_estimate")
                    done = data.get("downloaded_bytes", 0)
                    if total:
                        queue.update_progress(jid, identity, "downloading", int(done * 100 / total))

            path = await asyncio.to_thread(
                lambda: JavtifulProvider(max_attempts=2).download(
                    url, out, f"javtiful-{vid}", quality=job.get("quality"),
                    progress_hook=progress_hook,
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

        # FFprobe is independent of Telegram upload, so its failure cannot duplicate a video.
        media_stage = await asyncio.to_thread(queue.stage_done, jid, "media_probe")
        if not media_stage:
            probe = await asyncio.to_thread(subprocess.run, [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)
            ], capture_output=True, text=True, check=True)
            duration = float(probe.stdout.strip())
            await asyncio.to_thread(queue.complete_stage, jid, identity, "media_probe",
                                    {"duration_seconds": duration, "size": path.stat().st_size})
        else:
            duration = float(media_stage.get("duration_seconds") or 0)

        if not await asyncio.to_thread(queue.set_status, jid, identity, "uploading",
                                       output_path=str(path), lease_seconds=LEASE):
            raise RuntimeError("Job lease was lost before upload.")

        video_stage = await asyncio.to_thread(queue.stage_done, jid, "video_upload")
        if not video_stage:
            check_stop()
            size_mb = path.stat().st_size / 1048576
            await status_edit(chat_id, message_id, f"📤 Uploading {vid} ({size_mb:.1f} MiB) via Pyrogram MTProto…")
            sent = await app.send_video(chat_id=int(video_chat), video=str(path), caption=vid,
                                        supports_streaming=True, parse_mode=ParseMode.DISABLED)
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
                                            caption=f"{vid} | Screenshot {i+1}/{SHOT_COUNT}")
                await asyncio.to_thread(queue.complete_stage, jid, identity, stage_name,
                                        {"chat_id": sent.chat.id, "message_id": sent.id})
                sent_shots += 1
        else:
            await asyncio.to_thread(queue.complete_stage, jid, identity, "screenshots_skipped", {})
        check_stop()
        if not await asyncio.to_thread(queue.set_status, jid, identity, "completed",
                                       output_path=str(path), lease_seconds=LEASE):
            raise RuntimeError("Could not mark job completed because worker ownership was lost.")
        await status_edit(chat_id, message_id, f"✅ Completed {vid}. Screenshots sent: {sent_shots}")
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
                              f"{'⚠️ Requeued' if state == 'queued' else '❌ Failed'} {vid}: {error}")
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
    await message.reply_text(
        "TMJDLC Control Center · Pyrogram MTProto\n\n"
        "/panel — dashboard\n/dl POST_URL — queue one video\n"
        "/batch CHANNEL_URL [PAGES] — queue all or selected pages (e.g. 1-5,8)\n"
        "/queue — queue status\n/jobs — recent jobs\n"
        "/setup — guided destination setup\n/settings — configuration\n"
        "/storage — disk status\n/cancel JOB_ID — cancel a job\n/retry JOB_ID — retry failed job"
    )


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


async def batch_cmd(client, message):
    if await deny(message): return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        return await message.reply_text("Usage: /batch CHANNEL_URL [all|1-5,8]")
    url = parts[1].strip()
    selection = parts[2].strip().lower() if len(parts) > 2 else "all"
    try:
        info = await asyncio.to_thread(JavtifulChannelScraper().inspect, url)
        if selection == "all":
            pages = list(range(1, info.total_pages + 1))
        else:
            pages = []
            for token in selection.split(","):
                token = token.strip()
                if "-" in token:
                    first, last = (int(x) for x in token.split("-", 1))
                    if first > last:
                        raise ValueError("Page range must be ascending.")
                    pages.extend(range(first, last + 1))
                else:
                    pages.append(int(token))
            pages = sorted(set(pages))
            if not pages or min(pages) < 1 or max(pages) > info.total_pages:
                raise ValueError(f"Choose pages from 1 to {info.total_pages}.")
        status = await message.reply_text(
            f"📚 {info.name}\nPages: {', '.join(map(str, pages[:30]))}"
            f"{'…' if len(pages) > 30 else ''}\nCollecting links…"
        )
        urls = await asyncio.to_thread(JavtifulChannelScraper().videos_for_pages, info, pages)
        items = [(VIDEO_ID.search(u).group(1), u, 720, message.from_user.id, True)
                 for u in urls if VIDEO_ID.search(u)]
        added, skipped = await asyncio.to_thread(queue.enqueue_many, items, skip_existing=True)
        await status.edit_text(
            f"✅ Batch queued\nChannel: {info.name}\nPages: {len(pages)}\n"
            f"Found: {len(urls)}\nAdded: {added}\nSkipped: {skipped}"
        )
    except Exception as exc:
        await message.reply_text(f"Batch failed: {type(exc).__name__}: {str(exc)[:400]}")


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


async def text_handler(client, message):
    if await deny(message): return
    text = (message.text or "").strip()
    uid = message.from_user.id
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
            channel_state[uid] = {"info": info}
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Queue all pages", callback_data="channel:all"),
                InlineKeyboardButton("Cancel", callback_data="channel:cancel"),
            ]])
            await status.edit_text(
                f"📚 {info.name}\nVideos: {info.total_videos}\nPages: {info.total_pages}\n"
                f"Final page: {info.last_page_count}\nQuality: 720p",
                reply_markup=keyboard,
            )
        except Exception as exc:
            await status.edit_text(f"Channel inspection failed: {type(exc).__name__}: {str(exc)[:400]}")
        return
    await message.reply_text("Send /help to see available commands.")


async def callback_handler(client, callback):
    uid = callback.from_user.id
    if uid not in ADMINS:
        return await callback.answer("Not authorized.", show_alert=True)
    await callback.answer()
    if callback.data == "channel:cancel":
        channel_state.pop(uid, None)
        return await callback.message.edit_text("Channel queue cancelled.")
    state = channel_state.get(uid)
    if not state:
        return await callback.message.edit_text("Session expired. Send the channel URL again.")
    info = state["info"]
    await callback.message.edit_text("⏳ Collecting channel links…")
    try:
        urls = await asyncio.to_thread(
            JavtifulChannelScraper().videos_for_pages, info, list(range(1, info.total_pages + 1))
        )
        items = [(VIDEO_ID.search(url).group(1), url, 720, uid, True) for url in urls]
        added, skipped = await asyncio.to_thread(queue.enqueue_many, items, skip_existing=True)
        await callback.message.edit_text(
            f"✅ Batch queued\nVideos found: {len(urls)}\nAdded: {added}\nSkipped existing: {skipped}"
        )
    except Exception as exc:
        await callback.message.edit_text(f"❌ Queue failed: {type(exc).__name__}: {str(exc)[:400]}")
    finally:
        channel_state.pop(uid, None)


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
app.add_handler(CallbackQueryHandler(callback_handler, filters.regex(r"^channel:")))
app.add_handler(MessageHandler(text_handler, filters.text & filters.private & ~filters.command))


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
