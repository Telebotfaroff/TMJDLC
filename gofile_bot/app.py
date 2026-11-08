from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from gofile_bot.gofile import GoFileClient, GoFileError
from gofile_bot.state import StateStore


LOG = logging.getLogger("tmjdlc.gofile")
URL_RE = re.compile(r"https?://(?:www\.)?javtiful\.com/[^\s<>]+", re.I)
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

    def send(self, text: str) -> None:
        self.call("sendMessage", chat_id=self.owner_id, text=text, disable_web_page_preview=False)

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


def _download_javtiful(url: str, work_dir: Path, progress) -> Path:
    # Reuse only the existing Javtiful resolver/downloader; no legacy bot, DB or queue.
    from tmjdlc.providers.javtiful import JavtifulProvider

    provider = JavtifulProvider(progress_callback=progress, max_attempts=2)
    stem = f"video_{int(time.time())}"
    path, _stream = provider.download(url, work_dir, stem, quality=720)
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

    def save_state() -> None:
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
        if int(state.get("video_count", 0)) > 0:
            bot.send("Finish the current folder with /finish before changing GoFile accounts.")
            return
        previous = {
            key: state.get(key)
            for key in ("gofile_token", "root_folder_id", "folder_id", "folder_code", "video_count")
        }
        state["gofile_token"] = api_key.strip()
        state["root_folder_id"] = ""
        state["folder_id"] = ""
        state["folder_code"] = ""
        state["video_count"] = 0
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

    def close_current_folder() -> tuple[int, str]:
        count = int(state.get("video_count", 0))
        link = gofile.current_folder_link()
        if count and link:
            state["completed_folders"].append({"count": count, "link": link})
            state.update({"folder_id": "", "folder_code": "", "video_count": 0})
            save_state()
        return count, link

    LOG.info("TMJDLC GoFile controller starting; state=%s", state_path)
    bot.send("TMJDLC GoFile bot is online. Send a Javtiful video URL to download and upload it.")

    while True:
        try:
            updates = bot.poll()
            for update in updates:
                state["telegram_offset"] = int(update["update_id"])
                save_state()
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                sender = message.get("from") or {}
                if int(sender.get("id", 0)) != owner_id or int(chat.get("id", 0)) != owner_id:
                    continue
                text = (message.get("text") or "").strip()

                # API-key setup is a two-step private-chat flow. The key is never
                # echoed or logged, and its Telegram message is deleted after receipt.
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
                        "Send a Javtiful video URL to process it.\n"
                        "/status — current folder progress\n"
                        "/finish — publish the current partial folder link\n"
                        "/setgofilekey — configure your GoFile API key"
                    )
                    continue
                if text.startswith("/status"):
                    link = gofile.current_folder_link() or "No folder created yet"
                    bot.send(
                        f"Folder: {state.get('folder_number', 0)}\n"
                        f"Videos: {state.get('video_count', 0)}/{VIDEOS_PER_FOLDER}\n{link}"
                    )
                    continue
                if text.startswith("/finish"):
                    count = int(state.get("video_count", 0))
                    link = gofile.current_folder_link()
                    if count and link:
                        count, link = close_current_folder()
                        bot.send(f"GoFile folder completed ({count} videos):\n{link}")
                    else:
                        bot.send("There are no videos in the current folder.")
                    continue

                match = URL_RE.search(text)
                if not match:
                    continue
                url = match.group(0).rstrip(".,)")
                bot.send("Download started. This bot processes one link at a time.")

                def progress(detail: str) -> None:
                    LOG.info("%s", detail)

                try:
                    media_path = _download_javtiful(url, work_dir, progress)
                    bot.send(f"Download complete: {media_path.name}\nUploading to GoFile…")
                    result = gofile.upload(media_path)
                    state["video_count"] = int(state.get("video_count", 0)) + 1
                    save_state()
                    count = int(state["video_count"])
                    bot.send(f"GoFile upload successful ({count}/{VIDEOS_PER_FOLDER}).")
                    if count >= VIDEOS_PER_FOLDER:
                        count, link = close_current_folder()
                        bot.send(f"GoFile folder completed ({count} videos):\n{link}")
                    try:
                        media_path.unlink(missing_ok=True)
                    except OSError:
                        LOG.warning("Could not clean downloaded file: %s", media_path)
                except Exception as exc:
                    LOG.exception("Request failed")
                    bot.send(f"Request failed: {type(exc).__name__}: {str(exc)[:500]}")
        except KeyboardInterrupt:
            LOG.info("Stopping on keyboard interrupt")
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
