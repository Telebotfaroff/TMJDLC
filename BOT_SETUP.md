# TMJDLC Telegram Bot

TMJDLC uses **Pyrogram/MTProto** for Telegram updates, commands, callbacks, and video uploads. The durable queue runs on local SQLite for transactional worker coordination and synchronizes compressed snapshots to the private GitHub data repository.

## GitHub data repository

Use the private repository:

- Repository: `gitroff/Database`
- Branch: `main`
- Colab environment: only `GITHUB_TOKEN`
- Required token permission: **Contents — Read and write**

On first startup, TMJDLC creates:

- `config/secrets.json` — primary Telegram credentials, optional secondary upload-only bot credentials, admin IDs, optional helper bot tokens, and optional GoFile token.
- `config/bot.json` — default worker and runtime settings.
- `config/commands.json` — customizable Telegram menu labels, visibility, and order.
- `catalogue/channels.json` — saved Javtiful channel catalogue.
- `database/tmjdlc.sqlite3.gz` — compressed durable SQLite snapshot.
- `uploads/records/<job-id>.json` — complete per-upload receipts and metadata.

After first startup, open `config/secrets.json` in the private repository and enter your Telegram credentials. For the secondary uploader, fill all three fields: `TG_SECONDARY_BOT_TOKEN`, `TG_SECONDARY_API_ID`, and `TG_SECONDARY_API_HASH`. Add both the primary and secondary bots as administrators in the destination channel with permission to post messages. The secondary bot is upload-only; it does not need to be a control bot. Do not add `GITHUB_TOKEN` to the repository.

Only one Colab runtime should write to this data repository at a time. SQLite handles concurrent workers inside that runtime; GitHub snapshots are synchronized periodically and after critical upload receipts.

## Install and run in Colab

Open the repository notebook and add `GITHUB_TOKEN` to Colab Secrets. Then run the cells in order.

```bash
pip install -e .
sudo apt-get update -qq && sudo apt-get install -y ffmpeg aria2
python -m playwright install --with-deps chromium
python -m tmjdlc.bot
```

The primary bot token receives updates and handles control commands. Telegram uploads are distributed across the primary upload client and any eligible upload-only helper clients, including the optional secondary bot configured with its own API ID/hash. Videos larger than 1,950,000,000 bytes are automatically split by FFmpeg into sequential, independently playable MP4 parts. Each part is uploaded below Telegram's 2 GB limit, with part numbering in its caption and separate upload receipts so interrupted jobs can resume without re-uploading completed parts.

## Commands

- `/help` — show command and feature instructions.

- Send a Javtiful channel URL or use `/batch CHANNEL_URL` to inspect pages and select video counts.
- `/dl POST_URL` — enqueue one video.
- `/queue` — show queue counts and recent jobs.
- `/processes` — live download/upload workers, progress, and upload backlog.
- `/channels` — scan Javtiful for channel URLs and browse the saved catalogue.
- `/commands` — show/hide, rename, and reorder menu buttons.
- `/cancel JOB_ID` — cancel a job.
- `/retry JOB_ID` — requeue a failed job.
- `/setup` and `/setupload CHANNEL_ID` — configure the destination.
- `/settings` — configure worker counts and upload settings.

## Worker behavior

- Hard maximum: two download workers and four upload workers.
- Download and upload concurrency can be selected independently in Telegram (1–4 upload workers).
- Background Download is enabled by default. Download workers can resolve Playwright streams and prepare upcoming videos while all upload slots are busy; the ready-to-upload backlog remains bounded by the configured per-worker backlog setting.
- Upload backlog allowance is configurable from Settings: 0, 1, or 2 waiting videos per active upload worker.
- Download claims stop when active uploads plus the configured pending-upload capacity is full.
- Worker leases and retry state are persisted in SQLite and synchronized to GitHub.
- Complete Telegram and GoFile upload metadata is saved as individual JSON records.
- Videos larger than 1,950,000,000 bytes are split into independently playable MP4 parts before Telegram upload.
- Each part is uploaded sequentially and its Telegram receipt is persisted independently for safe resume.
- Downloaded media and generated parts stay on Colab storage and are cleaned after successful delivery when enabled.

Keep the Colab runtime running while jobs process. Colab media storage is temporary; queue state, settings, channel catalogue, and completed upload records are stored in the private GitHub data repository.
