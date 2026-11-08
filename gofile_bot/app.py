from __future__ import annotations

import logging
import os
import re
import time
import threading
from pathlib import Path
from typing import Any

from tmjdlc.providers.javtiful.channel import JavtifulChannelScraper

import requests
from dotenv import load_dotenv

from gofile_bot.gofile import GoFileClient, GoFileError
from gofile_bot.state import StateStore
from gofile_bot.pipeline import DownloadUploadPipeline


LOG = logging.getLogger("tmjdlc.gofile")
URL_RE = re.compile(r"https?://(?:www\.)?javtiful\.com/[^\s<>]+", re.I)
CHANNEL_URL_RE = re.compile(r"https?://(?:www\.)?javtiful\.com/channel/[A-Za-z0-9_-]+/?", re.I)
VIDEOS_PER_FOLDER = 20


class TelegramBot:
    def __init__(self, token: str, owner_id: int, state: dict[str, Any], save_state) -> None:
        self.token = token
        self.owner_id = owner_id
        self.state = state
        self.save_state = save_state
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, **params: Any) -> Any:
        response = requests.post(f"{self.base}/{method}", json=params, timeout=70)
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {payload.get('description', 'unknown error')}")
        return payload.get("result")

    def send(self, text: str) -> dict[str, Any]:
        return self.call("sendMessage", chat_id=self.owner_id, text=text, disable_web_page_preview=False)

    def edit(self, message_id: int, text: str) -> None:
        self.call("editMessageText", chat_id=self.owner_id, message_id=message_id, text=text, disable_web_page_preview=True)

    def delete_message(self, message_id: int) -> None:
        self.call("deleteMessage", chat_id=self.owner_id, message_id=message_id)

    def poll(self) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            offset=int(self.state.get("telegram_offset", 0)) + 1,
            timeout=50,
            allowed_updates=["message"],
        )
        return result or []


def _download_javtiful(url: str, work_dir: Path, progress, progress_hook=None) -> Path:
    # Reuse only the existing Javtiful resolver/downloader; no legacy bot, DB or queue.
    from tmjdlc.providers.javtiful import JavtifulProvider

    provider = JavtifulProvider(progress_callback=progress, max_attempts=2)
    stem = f"video_{int(time.time())}"
    path, _stream = provider.download(
        url, work_dir, stem, quality=720, progress_hook=progress_hook
    )
    return path


def run() -> None:
    load_dotenv()
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    owner_raw = os.environ.get("TG_OWNER_ID", "").strip()
    api_token = os.environ.get("GOFILE_API_TOKEN", "").strip()
    work_dir = Path(os.environ.get("TMJDLC_WORK_DIR", "/content/tmjdlc-gofile"))
    state_path = Path(os.environ.get("TMJDLC_STATE_FILE", "/content/TMJDLC/data/gofile_state.json"))
    if not token or not owner_raw:
        raise RuntimeError("Set TG_BOT_TOKEN and TG_OWNER_ID in Colab Secrets or environment")
    owner_id = int(owner_raw)
    work_dir.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_path)
    state = store.load()
    state.setdefault("completed_folders", [])

    state_lock = threading.RLock()

    def save_state() -> None:
        with state_lock:
            store.save(state)

    bot = TelegramBot(token, owner_id, state, save_state)
    gofile = GoFileClient(state, save_state, api_token)

    def configure_gofile_key(api_key: str, message_id: int) -> None:
        # Remove the Telegram message first so the credential is not left in chat history.
        try:
            bot.delete_message(message_id)
        except Exception:
            LOG.warning("Could not delete the message containing the GoFile API key")
        if not api_key or api_key.startswith("/"):
            bot.send("No API key was saved. Send /setgofilekey and then send the key.")
            return
        if int(state.get("video_count", 0)) > 0 or int(state.get("folder_reserved_count", 0)) > 0 or pipeline.active_jobs > 0:
            bot.send("Finish the current folder with /finish after active downloads/uploads complete before changing GoFile accounts.")
            return
        previous = {
            key: state.get(key)
            for key in ("gofile_token", "root_folder_id", "folder_id", "folder_code", "video_count", "folder_reserved_count")
        }
        state["gofile_token"] = api_key.strip()
        state["root_folder_id"] = ""
        state["folder_id"] = ""
        state["folder_code"] = ""
        state["video_count"] = 0
        state["folder_reserved_count"] = 0
        save_state()
        try:
            gofile.ensure_account()
            bot.send("GoFile API key verified successfully. This account will be used for future uploads.")
        except Exception:
            for key, value in previous.items():
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = value
            save_state()
            LOG.exception("GoFile API-key verification failed")
            bot.send("GoFile could not verify that API key. Check that it is the account API token, then send /setgofilekey to retry.")

    def folder_completed(count: int, link: str) -> None:
        bot.send(f"GoFile folder completed ({count} videos):\n{link}")

    pipeline = DownloadUploadPipeline(
        gofile, state, save_state, work_dir, folder_completed,
        download_workers=2, upload_workers=2, state_lock=state_lock,
    )
    channel_scraper = JavtifulChannelScraper()
    pending_channel = None

    # One live dashboard message lists up to two downloads and two uploads.
    dashboard_lock = threading.RLock()
    dashboard_id = None
    dashboard_last_edit = 0.0
    dashboard_jobs: dict[str, dict[str, Any]] = {}
    dashboard_counter = 0

    def _duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

    def _format_size(size: int) -> str:
        value = float(max(0, size))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return "0 B"

    def _progress_bar(done: int, total: int, width: int = 12) -> str:
        if total <= 0:
            return "░" * width + " 0%"
        ratio = min(1.0, max(0.0, done / total))
        filled = min(width, int(ratio * width))
        return f"{'█' * filled}{'░' * (width - filled)} {ratio * 100:.1f}%"

    def _dashboard_text() -> str:
        now = time.monotonic()
        downloading = [j for j in dashboard_jobs.values() if j["phase"] == "downloading"]
        uploading = [j for j in dashboard_jobs.values() if j["phase"] == "uploading"]
        lines = ["📥 Downloading"]
        if not downloading:
            lines.append("No active downloads")
        for i, job in enumerate(sorted(downloading, key=lambda x: x["order"])[:2], 1):
            lines.append(f"{i}. {job['title']}")
            lines.append(
                f"{_progress_bar(job.get('done', 0), job.get('total', 0))}  "
                f"{_format_size(job.get('done', 0))} / "
                f"{_format_size(job.get('total', 0)) if job.get('total', 0) else 'size unknown'}"
            )
            lines.append(
                f"Speed - {job['speed']} | ETA - {job['eta']} | "
                f"Elapsed - {_duration(now - job['phase_started'])}"
            )
        lines.append("")
        lines.append("📤 Uploading")
        if not uploading:
            lines.append("No active uploads")
        for i, job in enumerate(sorted(uploading, key=lambda x: x["order"])[:2], 1):
            lines.append(f"{i}. {job['title']}")
            lines.append(
                f"{_progress_bar(job.get('done', 0), job.get('total', 0))}  "
                f"{_format_size(job.get('done', 0))} / {_format_size(job.get('total', 0))}"
            )
            lines.append(
                f"Speed - {job['speed']} | ETA - {job['eta']} | "
                f"Elapsed - {_duration(now - job['phase_started'])}"
            )
        return "\n".join(lines)[:3900]

    def refresh_dashboard(force: bool = False) -> None:
        nonlocal dashboard_id, dashboard_last_edit
        with dashboard_lock:
            now = time.monotonic()
            if dashboard_id is None:
                result = bot.send(_dashboard_text())
                dashboard_id = int(result["message_id"])
                dashboard_last_edit = now
                return
            if not force and now - dashboard_last_edit < 1.5:
                return
            try:
                bot.edit(dashboard_id, _dashboard_text())
                dashboard_last_edit = now
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    LOG.debug("Dashboard update failed: %s", exc)

    def start_video_job(url: str, label: str = "") -> None:
        nonlocal dashboard_counter
        dashboard_counter += 1
        job_id = str(dashboard_counter)
        match_code = re.search(r"/(?:video|videos|post)/([^/?#]+)", url, re.I)
        code = match_code.group(1) if match_code else (url.rstrip("/").split("/")[-1] or "Javtiful")
        with dashboard_lock:
            dashboard_jobs[job_id] = {
                "order": dashboard_counter,
                "title": code,
                "phase": "downloading",
                "speed": "Starting…",
                "eta": "Calculating…",
                "phase_started": time.monotonic(),
                "last_upload_bytes": 0,
                "last_upload_at": time.monotonic(),
                "done": 0,
                "total": 0,
            }
        refresh_dashboard(force=True)

        def progress(detail: str) -> None:
            with dashboard_lock:
                job = dashboard_jobs.get(job_id)
                if not job:
                    return
                if detail.startswith("TITLE|"):
                    title = re.sub(r"\s+", " ", detail.split("|", 1)[1]).strip()
                    title = title[:58] or code
                    job["title"] = f"{code} · {title}"
                elif detail.startswith("UPLOAD|"):
                    try:
                        sent, total = (int(v) for v in detail.split("|")[1:3])
                        now = time.monotonic()
                        elapsed = max(0.001, now - job["last_upload_at"])
                        rate = max(0, sent - job["last_upload_bytes"]) / elapsed
                        job["last_upload_bytes"] = sent
                        job["last_upload_at"] = now
                        job["done"] = sent
                        job["total"] = total
                        job["speed"] = f"{rate / (1024 * 1024):.2f} MB/s"
                        job["eta"] = _duration((total - sent) / rate) if rate > 0 and total >= sent else "Calculating…"
                        if job["phase"] != "uploading":
                            job["phase"] = "uploading"
                            job["phase_started"] = now
                    except (ValueError, IndexError):
                        pass
                elif detail.startswith("DOWNLOAD|"):
                    bits = detail.split("|")
                    if len(bits) > 1 and bits[1]:
                        job["speed"] = bits[1]
                    if len(bits) > 2 and bits[2]:
                        job["eta"] = bits[2]
                    if len(bits) > 3:
                        try:
                            job["done"] = int(bits[3] or 0)
                            job["total"] = int(bits[4] or 0) if len(bits) > 4 else 0
                        except ValueError:
                            pass
                    job["phase"] = "downloading"
                elif "Waiting for GoFile upload" in detail:
                    job["phase"] = "uploading"
                    job["phase_started"] = time.monotonic()
                    job["speed"] = "Starting…"
                    job["eta"] = "Calculating…"
                    job["done"] = 0
                    job["total"] = 0
                elif "Uploading " in detail:
                    job["phase"] = "uploading"
                    job["phase_started"] = time.monotonic()
                    job["speed"] = "Starting…"
                    job["eta"] = "Calculating…"
                    job["done"] = 0
                    job["total"] = 0
            refresh_dashboard()

        def download_hook(data: dict[str, Any]) -> None:
            if data.get("status") == "downloading":
                speed = str(data.get("_speed_str") or "").strip() or "Calculating…"
                eta = str(data.get("_eta_str") or "").strip() or "Calculating…"
                downloaded = int(data.get("downloaded_bytes") or 0)
                total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
                progress(f"DOWNLOAD|{speed}|{eta}|{downloaded}|{total}")
            elif data.get("status") == "finished":
                filename = data.get("filename") or (data.get("info_dict") or {}).get("_filename")
                try:
                    final_size = Path(filename).stat().st_size if filename else 0
                except OSError:
                    final_size = 0
                if final_size:
                    progress(f"DOWNLOAD|Complete|00:00|{final_size}|{final_size}")
                progress("Download finished. Finalizing video…")

        future = pipeline.submit(url, progress, download_hook)

        def on_done(done) -> None:
            try:
                done.result()
                with dashboard_lock:
                    dashboard_jobs.pop(job_id, None)
            except Exception as exc:
                LOG.exception("Video job failed")
                try:
                    bot.send(f"❌ {code} failed: {type(exc).__name__}: {str(exc)[:1600]}")
                except Exception:
                    pass
                with dashboard_lock:
                    dashboard_jobs.pop(job_id, None)
            refresh_dashboard(force=True)

        future.add_done_callback(on_done)

    LOG.info("TMJDLC GoFile controller starting; state=%s", state_path)
    bot.send(
        "TMJDLC GoFile bot is online. Send a Javtiful video or channel URL.\n"
        "Channel downloads support all videos or selected pages.\n"
        "Concurrency: 2 downloads + 2 GoFile uploads."
    )

    while True:
        try:
            updates = bot.poll()
            for update in updates:
                with state_lock:
                    state["telegram_offset"] = int(update["update_id"])
                    save_state()
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                sender = message.get("from") or {}
                if int(sender.get("id", 0)) != owner_id or int(chat.get("id", 0)) != owner_id:
                    continue
                text = (message.get("text") or "").strip()

                if state.get("awaiting_gofile_key"):
                    state["awaiting_gofile_key"] = False
                    save_state()
                    configure_gofile_key(text, int(message.get("message_id", 0)))
                    continue

                if text.startswith("/setgofilekey"):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 2:
                        configure_gofile_key(parts[1].strip(), int(message.get("message_id", 0)))
                    else:
                        state["awaiting_gofile_key"] = True
                        save_state()
                        bot.send("Send your GoFile API key in your next private message. I will delete that message after receiving it.")
                    continue

                if text.startswith("/start"):
                    bot.send(
                        "TMJDLC GoFile Controller\n"
                        "Send a Javtiful video or channel URL.\n"
                        "/status — current folder progress\n"
                        "/finish — publish the current partial folder\n"
                        "/setgofilekey — configure your GoFile API key"
                    )
                    continue

                if text.startswith("/status"):
                    bot.send(
                        f"Folder: {state.get('folder_number', 0)}\n"
                        f"Uploaded: {state.get('video_count', 0)}/{VIDEOS_PER_FOLDER}\n"
                        f"Reserved upload slots: {state.get('folder_reserved_count', state.get('video_count', 0))}/{VIDEOS_PER_FOLDER}\n"
                        f"{gofile.current_folder_link() or 'No active folder'}"
                    )
                    continue

                if text.startswith("/finish"):
                    count, link, finished = pipeline.finish_current_folder()
                    if not finished:
                        bot.send("Downloads or uploads are still running. Try /finish again after all active jobs complete.")
                    elif count and link:
                        bot.send(f"GoFile folder completed ({count} videos):\n{link}")
                    else:
                        bot.send("There are no completed videos in the current folder.")
                    continue

                # Process the response to a channel inspection.
                if pending_channel is not None and not text.startswith("/"):
                    info = pending_channel
                    choice = text.lower().strip()
                    pages: list[int] = []
                    if choice in {"all", "all videos", "all pages"}:
                        pages = list(range(1, info.total_pages + 1))
                    elif choice.startswith("pages "):
                        try:
                            for token_part in choice[6:].split(","):
                                token_part = token_part.strip()
                                if "-" in token_part:
                                    first, last = (int(v.strip()) for v in token_part.split("-", 1))
                                    if first > last:
                                        raise ValueError
                                    pages.extend(range(first, last + 1))
                                else:
                                    pages.append(int(token_part))
                            pages = sorted(set(pages))
                            if not pages or any(p < 1 or p > info.total_pages for p in pages):
                                raise ValueError
                        except ValueError:
                            bot.send(f"Invalid selection. Use all, pages 1-3, or pages 1,3. Available pages: 1-{info.total_pages}.")
                            continue
                    else:
                        bot.send(f"Reply with all, pages 1-3, or pages 1,3. Available pages: 1-{info.total_pages}.")
                        continue

                    pending_channel = None
                    bot.send(f"Reading {len(pages)} selected page(s)…")
                    try:
                        video_urls = channel_scraper.videos_for_pages(info, pages)
                    except Exception as exc:
                        bot.send(f"Could not read channel pages: {type(exc).__name__}: {str(exc)[:350]}")
                        continue
                    if not video_urls:
                        bot.send("No videos were found on those pages.")
                        continue
                    bot.send(
                        f"Found {len(video_urls)} videos from {info.name}. "
                        "Starting the concurrent download/upload pipeline."
                    )
                    for index, video_url in enumerate(video_urls, 1):
                        start_video_job(video_url, f"{info.name} · {index}/{len(video_urls)}")
                    continue

                channel_match = CHANNEL_URL_RE.search(text)
                if channel_match:
                    channel_url = channel_match.group(0).rstrip(".,)")
                    try:
                        info = channel_scraper.inspect(channel_url)
                        pending_channel = info
                        bot.send(
                            f"📺 Channel: {info.name}\n"
                            f"Videos: {info.total_videos}\n"
                            f"Pages: {info.total_pages}\n\n"
                            "Reply with one of these:\n"
                            "• all — download every video\n"
                            "• pages 1-3 — download a page range\n"
                            "• pages 1,3,5 — download individual pages"
                        )
                    except Exception as exc:
                        LOG.exception("Channel inspection failed")
                        bot.send(f"Channel inspection failed: {type(exc).__name__}: {str(exc)[:400]}")
                    continue

                match = URL_RE.search(text)
                if match:
                    start_video_job(match.group(0).rstrip(".,)"))

        except KeyboardInterrupt:
            LOG.info("Stopping on keyboard interrupt")
            pipeline.shutdown()
            return
        except (requests.RequestException, GoFileError, RuntimeError, ValueError) as exc:
            LOG.exception("Polling/worker error")
            try:
                bot.send(f"Temporary error: {type(exc).__name__}: {str(exc)[:400]}")
            except Exception:
                pass
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("TMJDLC_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    run()
