# TMJDLC Telegram Bot

The bot uses **Pyrogram (MTProto) for every Telegram operation**: receiving updates, commands, callbacks, sending messages, and uploading large videos/photos. It does not use Bot API long polling or webhooks. A Neon PostgreSQL database stores the durable queue and channel settings.

## Environment

Keep these values in Colab Secrets or deployment environment variables; never commit credentials:

```env
TG_BOT_TOKEN=123456:your_bot_token
TG_API_ID=123456
TG_API_HASH=your_api_hash
TG_ADMIN_IDS=123456789
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB?sslmode=require
TMJDLC_MAX_PARALLEL=2
TMJDLC_JOB_MAX_ATTEMPTS=3
TMJDLC_JOB_LEASE_SECONDS=3600
TMJDLC_WORK_DIR=/content/tmjdlc-bot
```

Use the Neon connection string for the dedicated `Javtiful-uploader` database in the `Javtiful` project for `DATABASE_URL`. Keep it private.

## Install and run in Colab

```bash
pip install -e .
sudo apt-get update -qq && sudo apt-get install -y ffmpeg
python -m playwright install --with-deps chromium
python -m tmjdlc.bot
```

The bot uses a Pyrogram bot session created from `TG_API_ID`, `TG_API_HASH`, and `TG_BOT_TOKEN`. It does not require a personal-user login or session string. Pyrogram handles updates and uploads through MTProto; no `getUpdates` polling or webhook endpoint is used.

The bot initializes `tmjdlc_bot_jobs` and `tmjdlc_bot_settings`. The same additive schema is in `sql/001_tmjdlc_bot_queue.sql`; existing unrelated tables are not modified.

## Commands

- Send a Javtiful channel URL to inspect it and queue all pages.
- `/dl POST_URL` — enqueue one Javtiful post at 720p.
- `/queue` — show queue counts and recent jobs.
- `/cancel JOB_ID` — cancel a queued or failed job.
- `/retry JOB_ID` — requeue a failed job.
- `/setup` — guided setup for video and screenshot destination channels; send `SKIP` to disable screenshots.
- `/setupload CHANNEL_ID` — configure the video destination manually.
- `/setimages CHANNEL_ID` — configure the screenshot destination manually.
- `/settings` — show saved destinations and worker settings.

## Queue behavior

- PostgreSQL is the source of truth; jobs survive bot process/runtime restarts.
- Workers claim jobs with PostgreSQL `FOR UPDATE SKIP LOCKED`.
- Unique `video_id` prevents duplicate queue entries.
- Failed jobs retry up to `TMJDLC_JOB_MAX_ATTEMPTS`; `/retry` can manually requeue failed jobs.
- Worker leases recover abandoned jobs after a runtime restart.
- `TMJDLC_MAX_PARALLEL` controls concurrent jobs (1–4).
- Media and generated screenshots remain on local worker disk; Colab resets remove local files, but Neon retains job history.

## Telegram permissions

Add the bot as an administrator to both destination channels with permission to post. Only IDs listed in `TG_ADMIN_IDS` can use the private bot.

## Operational note

Keep the Colab runtime running while jobs process. The queue is durable, but Colab is an ephemeral worker host.
