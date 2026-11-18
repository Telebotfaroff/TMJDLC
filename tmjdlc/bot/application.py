"""Telegram Bot API interface for channel and video selection."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tmjdlc.config import Settings
from tmjdlc.drive.manager import DriveManager
from tmjdlc.providers.javtiful import (
    JavtifulChannelScraper,
    JavtifulURLParser,
    parse_number_selection,
    select_videos,
)
from tmjdlc.storage.json_store import JsonStore
from tmjdlc.bot.pipeline import DrivePipeline

log = logging.getLogger(__name__)


def _is_admin(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    return bool(user and user.id in settings.admin_ids)


async def _reply(update: Update, text: str, **kwargs) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)


def build_application(settings: Settings, drive: DriveManager, store: JsonStore) -> Application:
    async def post_init(app: Application) -> None:
        pipeline = DrivePipeline(settings, drive, store, app.bot)
        app.bot_data["pipeline"] = pipeline
        await pipeline.start()

    async def post_shutdown(app: Application) -> None:
        pipeline = app.bot_data.get("pipeline")
        if pipeline:
            await pipeline.stop()

    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["store"] = store
    app.bot_data["scraper"] = JavtifulChannelScraper()
    app.bot_data["parser"] = JavtifulURLParser()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update, settings):
            await _reply(update, "Access denied.")
            return
        context.user_data.clear()
        await _reply(
            update,
            "🎬 TMJDLC · Google Drive edition\n\n"
            "Send a Javtiful channel URL to choose pages and videos, "
            "or send one video URL directly.\n\n"
            "Commands: /status · /cancel",
        )

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update, settings):
            return
        pipeline: DrivePipeline = context.application.bot_data["pipeline"]
        await _reply(update, pipeline.status_text())

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update, settings):
            return
        context.user_data.clear()
        await _reply(update, "Selection cancelled. Already queued jobs will continue.")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update, settings):
            return
        message = update.effective_message
        if not message or not message.text:
            return
        value = message.text.strip()
        stage = context.user_data.get("stage")

        if stage == "pages":
            info = context.user_data["channel_info"]
            try:
                pages = parse_number_selection(value, maximum=info.total_pages)
                videos = await asyncio.to_thread(
                    app.bot_data["scraper"].videos_for_pages, info, pages
                )
                if not videos:
                    raise ValueError("No video links were found on those pages.")
            except Exception as exc:
                await _reply(update, f"Couldn't scan those pages: {exc}\nTry again or /cancel.")
                return
            context.user_data["video_urls"] = videos
            context.user_data["stage"] = "videos"
            parser = app.bot_data["parser"]
            preview = []
            for index, url in enumerate(videos[:60], 1):
                code = parser.parse(url).code or "No code"
                preview.append(f"{index}. {code}")
            suffix = f"\n… and {len(videos) - 60} more" if len(videos) > 60 else ""
            await _reply(
                update,
                f"Found {len(videos)} videos across {len(pages)} selected page(s).\n"
                + "\n".join(preview)
                + suffix
                + "\n\nWhich videos should be queued? Send all, a number, or ranges "
                  "like 1,3,5-8.",
            )
            return

        if stage == "videos":
            urls = context.user_data["video_urls"]
            completed = set(store.read().get("videos", {}).keys())
            try:
                selection = select_videos(urls, value, completed_codes=completed)
            except Exception as exc:
                await _reply(update, f"Invalid video selection: {exc}\nTry again or /cancel.")
                return
            chosen = [(item.url, item.code) for item in selection.selected if item.code]
            if not chosen:
                await _reply(
                    update,
                    f"No new coded videos selected. Skipped duplicates: "
                    f"{len(selection.skipped_duplicates)}. Try another selection or /cancel.",
                )
                return
            context.user_data["chosen"] = chosen
            context.user_data["stage"] = "concurrency"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{n} parallel", callback_data=f"parallel:{n}")]
                for n in (1, 2, 3, 4, 5, 6)
            ])
            await _reply(
                update,
                f"Selected {len(chosen)} new video(s). "
                f"Skipped duplicates: {len(selection.skipped_duplicates)}.\n"
                "Choose the maximum number of parallel downloads:",
                reply_markup=keyboard,
            )
            return

        try:
            source = app.bot_data["parser"].parse(value)
        except ValueError:
            source = None
        if source and source.code:
            context.user_data["chosen"] = [(source.url, source.code)]
            context.user_data["stage"] = "concurrency"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{n} parallel", callback_data=f"parallel:{n}")]
                for n in (1, 2, 3, 4, 5, 6)
            ])
            await _reply(update, f"Video detected: {source.code}\nChoose download concurrency:", reply_markup=keyboard)
            return

        try:
            info = await asyncio.to_thread(app.bot_data["scraper"].inspect, value)
        except Exception as exc:
            await _reply(update, f"Couldn't inspect that channel: {exc}")
            return
        context.user_data["channel_info"] = info
        context.user_data["stage"] = "pages"
        await _reply(
            update,
            f"📺 {info.name}\n"
            f"Videos: {info.total_videos}\n"
            f"Pages: {info.total_pages}\n"
            f"Last page videos: {info.last_page_count}\n\n"
            "Which pages should I scan? Send all, one page, or ranges "
            "like 1,3,5-8.",
        )

    async def choose_parallel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()
        if not _is_admin(update, settings):
            await query.edit_message_text("Access denied.")
            return
        stage = context.user_data.get("stage")
        if stage != "concurrency":
            await query.edit_message_text("This selection has expired. Send /start to begin again.")
            return
        try:
            limit = int((query.data or "").split(":", 1)[1])
            if not 1 <= limit <= 6:
                raise ValueError
        except (ValueError, IndexError):
            await query.edit_message_text("Invalid concurrency option.")
            return
        pipeline: DrivePipeline = context.application.bot_data["pipeline"]
        await pipeline.set_download_limit(limit)
        queued, skipped = await pipeline.submit(
            update.effective_chat.id, context.user_data.get("chosen", [])
        )
        context.user_data.clear()
        text = f"✅ Queued {len(queued)} job(s).\nDownload concurrency: {limit}"
        if skipped:
            text += "\nSkipped: " + ", ".join(skipped[:20])
        text += "\nUse /status to check progress."
        await query.edit_message_text(text)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(choose_parallel, pattern=r"^parallel:[1-6]$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def run_bot() -> None:
    settings = Settings.from_env()
    library_root = Path("/content/drive/MyDrive") / settings.drive_root_name
    if not library_root.exists():
        raise RuntimeError(
            "Google Drive is not mounted. In Colab run drive.mount('/content/drive') "
            "and initialize the TMJDLC library before starting the bot."
        )
    drive = DriveManager.from_colab(
        root_name=settings.drive_root_name, root_id=settings.drive_root_id
    )
    drive.ensure_root()
    store = JsonStore(library_root / "database.json")
    app = build_application(settings, drive, store)
    app.run_polling(drop_pending_updates=True)
