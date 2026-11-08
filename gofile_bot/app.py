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
        bot.send(f"GoFile folder completed ({count} videos):\\n{link}")

    pipeline = DownloadUploadPipeline(
        gofile, state, save_state, work_dir, folder_completed,
        download_workers=2, upload_workers=2, state_lock=state_lock,
    )
    channel_scraper = JavtifulChannelScraper()
    pending_channel = None

    def start_video_job(url: str, label: str = "") -> None:
        message_id = None
        last_update = 0.0
        last_text = ""

        def progress(detail: str) -> None:
            nonlocal last_update, last_text, message_id
            LOG.info("%s | %s", label or "video", detail)
            if message_id is None:
                status = bot.send(
                    f"⬇️ Starting {label or 'video'}…\\n"
                    "Pipeline: up to 2 downloads and 2 GoFile uploads simultaneously."
                )
                message_id = int(status["message_id"])
            now = time.monotonic()
            if now - last_update < 2.0 or detail == last_text:
                return
            try:
                prefix = f"{label}\\n" if label else ""
                bot.edit(message_id, f"{prefix}{detail[:850]}")
                last_update, last_text = now, detail
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    LOG.debug("Progress update failed: %s", exc)

        def download_hook(data: dict[str, Any]) -> None:
            if data.get("status") == "downloading":
                percent = str(data.get("_percent_str") or "").strip()
                speed = str(data.get("_speed_str") or "").strip()
                eta = str(data.get("_eta_str") or "").strip()
                downloaded = data.get("downloaded_bytes")
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                detail = percent or "Downloading…"
                if downloaded is not None and total:
                    try:
                        detail = f"{percent} | {float(downloaded)/(1024*1024):.1f}/{float(total)/(1024*1024):.1f} MB"
                    except (TypeError, ValueError):
                        pass
                if speed:
                    detail += f" | {speed}"
                if eta:
                    detail += f" | ETA {eta}"
                progress(detail)
            elif data.get("status") == "finished":
                progress("Download finished. Finalizing video…")

        future = pipeline.submit(url, progress, download_hook)

        def on_done(done) -> None:
            nonlocal message_id
            try:
                done.result()
                if message_id is None:
                    status = bot.send(f"✅ {label or 'Video'} completed.")
                    message_id = int(status["message_id"])
                else:
                    bot.edit(message_id, "✅ Download + GoFile upload completed.")
            except Exception as exc:
                LOG.exception("Video job failed")
                try:
                    if message_id is None:
                        bot.send(f"❌ {label or 'Video'} failed: {type(exc).__name__}: {str(exc)[:350]}")
                    else:
                        bot.edit(message_id, f"❌ Failed: {type(exc).__name__}: {str(exc)[:450]}")
                except Exception:
                    bot.send(f"❌ Failed: {type(exc).__name__}: {str(exc)[:450]}")

        future.add_done_callback(on_done)

    LOG.info("TMJDLC GoFile controller starting; state=%s", state_path)
    bot.send(
        "TMJDLC GoFile bot is online. Send a Javtiful video or channel URL.\\n"
        "Channel downloads support all videos or selected pages.\\n"
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
                        "TMJDLC GoFile Controller\\n"
                        "Send a Javtiful video or channel URL.\\n"
                        "/status — current folder progress\\n"
                        "/finish — publish the current partial folder\\n"
                        "/setgofilekey — configure your GoFile API key"
                    )
                    continue

                if text.startswith("/status"):
                    bot.send(
                        f"Folder: {state.get('folder_number', 0)}\\n"
                        f"Uploaded: {state.get('video_count', 0)}/{VIDEOS_PER_FOLDER}\\n"
                        f"Reserved upload slots: {state.get('folder_reserved_count', state.get('video_count', 0))}/{VIDEOS_PER_FOLDER}\\n"
                        f"{gofile.current_folder_link() or 'No active folder'}"
                    )
                    continue

                if text.startswith("/finish"):
                    count, link, finished = pipeline.finish_current_folder()
                    if not finished:
                        bot.send("Downloads or uploads are still running. Try /finish again after all active jobs complete.")
                    elif count and link:
                        bot.send(f"GoFile folder completed ({count} videos):\\n{link}")
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
                            f"📺 Channel: {info.name}\\n"
                            f"Videos: {info.total_videos}\\n"
                            f"Pages: {info.total_pages}\\n\\n"
                            "Reply with one of these:\\n"
                            "• all — download every video\\n"
                            "• pages 1-3 — download a page range\\n"
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
