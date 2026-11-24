# TMJDLC Telegram Bot

TMJDLC uses **Pyrogram/MTProto** for Telegram updates, commands, callbacks, and video uploads. The durable queue runs on local SQLite for transactional worker coordination and synchronizes compressed snapshots to the private GitHub data repository.

## GitHub data repository

Use the private repository:

- Repository: `gitroff/Database`
- Branch: `main`
- Colab environment: only `GITHUB_TOKEN`
- Required token permission: **Contents — Read and write**

On first startup, TMJDLC creates:

- `config/secrets.json` — Telegram bot token, API ID/hash, admin IDs, optional helper bot tokens, and optional GoFile token.
- `config/bot.json` — default worker and runtime settings.
- `catalogue/channels.json` — saved Javtiful channel catalogue.
- `database/tmjdlc.sqlite3.gz` — compressed durable SQLite snapshot.
- `uploads/YYYY/MM/<job-id>.json` — complete per-upload receipts and metadata.

After first startup, open `config/secrets.json` in the private repository and enter your Telegram credentials. Do not add `GITHUB_TOKEN` to the repository.

Only one Colab runtime should write to this data repository at a time. SQLite handles concurrent workers inside that runtime; GitHub snapshots are synchronized periodically and after critical upload receipts.

## Install and run in Colab

Open the repository notebook and add `GITHUB_TOKEN` to Colab Secrets. Then run the cells in order.

```bash
pip install -e .
sudo apt-get update -qq && sudo apt-get install -y ffmpeg aria2
python -m playwright install --with-deps chromium
python -m tmjdlc.bot
```

The bot does not require a personal-user login or session string. Uploads use the configured MTProto client; the actual upload account and its Telegram file-size allowance determine the maximum supported file size.

## Commands

- Send a Javtiful channel URL or use `/batch CHANNEL_URL` to inspect pages and select video counts.
- `/dl POST_URL` — enqueue one video.
- `/queue` — show queue counts and recent jobs.
- `/processes` — live download/upload workers, progress, and upload backlog.
- `/cancel JOB_ID` — cancel a job.
- `/retry JOB_ID` — requeue a failed job.
- `/setup` and `/setupload CHANNEL_ID` — configure the destination.
- `/settings` — configure worker counts and upload settings.

## Worker behavior

- Hard maximum: two download workers and two upload workers.
- Download and upload concurrency can be selected independently in Telegram.
- Upload backlog allowance is applied per active upload worker.
- Download claims stop when active uploads plus the configured pending-upload capacity is full.
- Worker leases and retry state are persisted in SQLite and synchronized to GitHub.
- Complete Telegram and GoFile upload metadata is saved as individual JSON records.
- Downloaded media stays on Colab storage and is cleaned after successful delivery when enabled.

Keep the Colab runtime running while jobs process. Colab media storage is temporary; queue state, settings, channel catalogue, and completed upload records are stored in the private GitHub data repository.
