# TMJDLC — Javtiful Telegram Downloader

[![Open TMJDLC in Google Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb)

TMJDLC is a private, Javtiful-only Telegram downloader built with Python, WZGram MTProto, yt-dlp, and Neon PostgreSQL. It downloads videos at **720p automatically**; there is no quality selector or provider selector.

## Telegram interface

Use `/start` to open the persistent menu:

- **Download Video** — send a Javtiful video URL; it is queued automatically at 720p.
- **Batch Download** — send a Javtiful channel URL, then choose new videos, all videos, latest videos, or custom pages.
- **Queue** — inspect recent jobs and use the existing cancellation/retry commands.
- **Settings / Upload Destination** — choose Telegram, GoFile, or both; GoFile reuses one persistent guest account and shared folder.
- **Settings** — configure the Telegram video destination, download workers, and 1- or 2-video simultaneous uploads.
- **Storage** and **Worker Status** — inspect runtime health.

The original command shortcuts remain available: `/dl`, `/batch`, `/setup`, `/setupload`, `/panel`, `/queue`, `/settings`, `/storage`, `/cancel`, and `/retry`.

## Run in Google Colab

Open the [TMJDLC notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb). Add `TG_BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH`, `TG_ADMIN_IDS`, and Neon `DATABASE_URL` to Colab Secrets, then run the notebook cells.

The bot uses WZGram (Pyrogram-compatible MTProto client) for Telegram updates and large media uploads. The upload client uses WZGram's concurrent-transmission setting. Optional WZML-X-style helper bot tokens can distribute channel uploads across eligible bot clients; each helper bot must be an administrator in the configured video channel. The main bot remains responsible for private-chat fallback. Neon stores durable jobs, job stages, and settings. Keep the Colab runtime running while jobs are processing.

## Worker behavior

- Automatic 720p selection for single and batch downloads.
- Durable Neon queue with retry and restart recovery.
- Separate download and upload progress reporting.
- Optional two-video concurrent uploads, controlled from Settings (default: one).
- Batch-scoped cancellation.
- Disk-space guard and automatic cleanup after successful upload.
- Javtiful stream resolution is refreshed before retrying a failed download.

## Configuration

Copy `.env.example` and provide the required Telegram and Neon credentials. Optional `TG_HELPER_BOT_TOKENS` accepts comma- or space-separated helper bot tokens. Keep it empty to use only the primary upload bot. FFmpeg and FFprobe are required for media validation and video thumbnail generation.

The worker pauses new downloads when free disk falls below `TMJDLC_MIN_FREE_DISK_GB` (default 3 GiB). Set `TMJDLC_AUTO_CLEAN=false` to disable local-file cleanup.

## Project architecture

- Provider: Javtiful only
- Bot/runtime: Python + WZGram MTProto
- Queue/database: Neon PostgreSQL
- Downloader: yt-dlp
- Browser-based stream detection: Playwright
- Runtime target: Google Colab


## GoFile uploads

Choose **Upload Destination** from the Telegram menu and select Telegram, GoFile, or Both. Set `GOFILE_API_TOKEN` in Colab Secrets or the runtime environment to use your own GoFile account token. If it is not supplied, TMJDLC creates one persistent guest account automatically. The token and account root folder are stored in Neon and reused after runtime restarts.

TMJDLC creates public folders named **TMJDLC Batch 001**, **TMJDLC Batch 002**, and so on. Each folder receives up to **20 successfully uploaded videos**. After video 20, the bot creates the next public folder and sends the completed folder link to every configured admin in Telegram. When the queue becomes fully idle, a final folder containing 1–19 videos is also sent and a fresh folder is prepared for the next queue run. Empty folders are not announced.

Uploads use streaming multipart requests. Folder IDs, share codes, counts, and notification receipts are persisted in Neon so worker restarts do not reset the batch counter. Every folder is created with `public: true`.

Keep `GOFILE_API_TOKEN` private: anyone holding it has full access to the GoFile account. Do not commit it to GitHub or include it in logs.
