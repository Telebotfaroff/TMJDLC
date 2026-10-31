# TMJDLC Telegram Bot

The bot is an admin-only Pyrogram controller with a durable PostgreSQL queue.

## Environment

Create a private `.env` file; never commit it:

```env
TG_API_ID=123456
TG_API_HASH=your_api_hash
TG_BOT_TOKEN=123456:your_bot_token
TG_ADMIN_IDS=123456789
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB?sslmode=require
TMJDLC_MAX_PARALLEL=2
TMJDLC_JOB_MAX_ATTEMPTS=3
TMJDLC_JOB_LEASE_SECONDS=3600
TMJDLC_WORK_DIR=/content/tmjdlc-bot
```

Use your Neon PostgreSQL connection string for `DATABASE_URL`. Keep it in Colab Secrets or deployment environment variables, not in notebook source or GitHub.

## Install and run in Colab

```bash
!pip install -e .
!sudo apt-get update -qq && sudo apt-get install -y ffmpeg
!python -m playwright install --with-deps chromium
!python -m tmjdlc.bot
```

The bot initializes two namespaced tables, `tmjdlc_bot_jobs` and `tmjdlc_bot_settings`. The same additive schema is checked into `sql/001_tmjdlc_bot_queue.sql`. It does not modify the existing Javtiful/Telegram tables.

## Commands

- `/dl POST_URL [720]` — enqueue one Javtiful post.
- `/channel @sourcechannel [limit]` — scan recent channel captions/text for Javtiful post URLs and enqueue unique posts.
- `/queue` — show queue counts and recent jobs.
- `/cancel JOB_ID` — cancel a queued or failed job.
- `/retry JOB_ID` — requeue a failed job.
- `/setupload CHANNEL_ID` — configure the video destination.
- `/setimages CHANNEL_ID` — configure the screenshot destination.
- `/settings` — show current destinations and worker settings.

## Queue behavior

- PostgreSQL is the source of truth; jobs survive bot process/runtime restarts.
- Workers claim jobs with PostgreSQL `FOR UPDATE SKIP LOCKED`, so multiple workers/instances do not claim the same queued job.
- Unique `video_id` prevents duplicate queue entries.
- Failed jobs retry up to `TMJDLC_JOB_MAX_ATTEMPTS`; `/retry` can manually requeue a failed job.
- Worker leases allow expired in-progress jobs to be recovered on startup.
- `TMJDLC_MAX_PARALLEL` controls worker count (1–4).
- Downloaded files and generated screenshots remain on the worker's local disk. The database stores job state and local output path, not video bytes. A Colab runtime reset can therefore remove local files even though job history remains persistent.

## Telegram permissions

Add the bot to the source channel with read access and to both destination channels as an administrator with permission to post. Only Telegram IDs listed in `TG_ADMIN_IDS` can use bot commands.

## Operational note

The bot process must remain running to process jobs. The database queue is durable across restarts, but Colab is an ephemeral worker; use a persistent worker host for continuous service.
