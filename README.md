# TMJDLC — Javtiful Telegram Downloader

[![Open TMJDLC in Google Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb)

TMJDLC is a private, Javtiful-only Telegram downloader built with Python, Pyrogram MTProto, yt-dlp, and Neon PostgreSQL. It downloads videos at **720p automatically**; there is no quality selector or provider selector.

## Telegram interface

Use `/start` to open the persistent menu:

- **Download Video** — send a Javtiful video URL; it is queued automatically at 720p.
- **Batch Download** — send a Javtiful channel URL, then choose new videos, all videos, latest videos, or custom pages.
- **Queue** — inspect recent jobs and use the existing cancellation/retry commands.
- **Settings** — configure video and screenshot destination channels.
- **Storage** and **Worker Status** — inspect runtime health.

The original command shortcuts remain available: `/dl`, `/batch`, `/setup`, `/setupload`, `/setimages`, `/panel`, `/queue`, `/settings`, `/storage`, `/cancel`, and `/retry`.

## Run in Google Colab

Open the [TMJDLC notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb). Add `TG_BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH`, `TG_ADMIN_IDS`, and Neon `DATABASE_URL` to Colab Secrets, then run the notebook cells.

The bot uses Pyrogram for Telegram updates and large media uploads. Neon stores durable jobs, job stages, and settings. Keep the Colab runtime running while jobs are processing.

## Worker behavior

- Automatic 720p selection for single and batch downloads.
- Durable Neon queue with retry and restart recovery.
- Separate download and upload progress reporting.
- Batch-scoped cancellation.
- Optional screenshot delivery.
- Disk-space guard and automatic cleanup after successful upload.
- Javtiful stream resolution is refreshed before retrying a failed download.

## Configuration

Copy `.env.example` and provide the required Telegram and Neon credentials. FFmpeg and FFprobe are required for media validation and screenshots.

The worker pauses new downloads when free disk falls below `TMJDLC_MIN_FREE_DISK_GB` (default 3 GiB). Set `TMJDLC_AUTO_CLEAN=false` to disable local-file cleanup.

## Project architecture

- Provider: Javtiful only
- Bot/runtime: Python + Pyrogram MTProto
- Queue/database: Neon PostgreSQL
- Downloader: yt-dlp
- Browser-based stream detection: Playwright
- Runtime target: Google Colab
