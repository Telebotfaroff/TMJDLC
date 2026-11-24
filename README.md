# TMJDLC — Javtiful Telegram Downloader

[![Open TMJDLC in Google Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb)

TMJDLC is a Javtiful Telegram downloader built with Python, WZGram MTProto, yt-dlp, SQLite, and a private GitHub data repository. It downloads videos at **720p automatically**; there is no quality selector or provider selector.

## Telegram interface

Use `/start` to open the persistent menu:

- **Download Video** — send a Javtiful video URL; it is queued automatically at 720p.
- **Batch Download** — send a Javtiful channel URL, then choose new videos, all videos, latest videos, or custom pages.
- **Queue** — inspect recent jobs and use the existing cancellation/retry commands.
- **Settings / Upload Destination** — choose Telegram, GoFile, or both; GoFile reuses one persistent guest account and shared folder.
- **Settings** — configure the Telegram video destination, download workers, and 1- or 2-video simultaneous uploads.
- **Storage** and **Worker Status** — inspect runtime health.
- **Live Processes** (`/processes`) — inspect active downloads, uploads, waiting upload backlog, worker IDs, progress, and free disk; use Refresh for an updated snapshot.

The original command shortcuts remain available: `/dl`, `/batch`, `/setup`, `/setupload`, `/panel`, `/queue`, `/settings`, `/storage`, `/cancel`, and `/retry`.

## Run in Google Colab

Open the [TMJDLC notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb). Add only `GITHUB_TOKEN` to Colab Secrets, then run the notebook cells.

The bot uses WZGram (Pyrogram-compatible MTProto client) for Telegram updates and large media uploads. The upload client uses WZGram's concurrent-transmission setting. Optional WZML-X-style helper bot tokens can distribute channel uploads across eligible bot clients; each helper bot must be an administrator in the configured video channel. The main bot remains responsible for private-chat fallback. A local SQLite queue provides transactional worker coordination; compressed database snapshots, configuration, channel catalogue, and completed upload records are stored in the private GitHub data repository. Keep the Colab runtime running while jobs are processing. A single active Colab runtime should write to the data repository at a time.

## Worker behavior

- Automatic 720p selection for single and batch downloads.
- Durable SQLite queue with retry and restart recovery, synchronized to GitHub snapshots.
- Separate download and upload progress reporting.
- One or two download workers and one or two upload workers, with a hard maximum of two each.
- Per-upload-worker backlog limits prevent uncontrolled downloads from filling Colab storage.
- Live process monitor (`/processes`) shows active downloads, uploads, waiting uploads, worker IDs, and progress.
- Batch-scoped cancellation.
- Disk-space guard and automatic cleanup after successful upload.
- Javtiful stream resolution is refreshed before retrying a failed download.

## Configuration

Set only `GITHUB_TOKEN` in Colab. The token must have Contents: Read and write access to the private `gitroff/Database` repository. On first startup, TMJDLC creates the repository layout; add Telegram credentials to `config/secrets.json` there. Bot settings are persisted through the data repository. Optional `TG_HELPER_BOT_TOKENS` accepts comma- or space-separated helper bot tokens. Keep it empty to use only the primary upload bot. FFmpeg and FFprobe are required for media validation and video thumbnail generation.

The worker pauses new downloads when free disk falls below `TMJDLC_MIN_FREE_DISK_GB` (default 3 GiB). Set `TMJDLC_AUTO_CLEAN=false` to disable local-file cleanup.

## Project architecture

- Provider: Javtiful only
- Bot/runtime: Python + WZGram MTProto
- Queue/database: SQLite with GitHub snapshot persistence
- Downloader: yt-dlp
- Browser-based stream detection: Playwright
- Runtime target: Google Colab


## GoFile uploads

Choose **Upload Destination** from the Telegram menu and select Telegram, GoFile, or Both. Set `GOFILE_API_TOKEN` in the private repository's `config/secrets.json` to use your own GoFile account token. If it is blank, TMJDLC creates one persistent guest account automatically. The token and account root folder are stored in the local SQLite database and synchronized to the private GitHub data repository.

TMJDLC creates public folders named **TMJDLC Batch 001**, **TMJDLC Batch 002**, and so on. Each folder receives up to **20 successfully uploaded videos**. After video 20, the bot creates the next public folder and sends the completed folder link to every configured admin in Telegram. When the queue becomes fully idle, a final folder containing 1–19 videos is also sent and a fresh folder is prepared for the next queue run. Empty folders are not announced.

Uploads use streaming multipart requests. Folder IDs, share codes, counts, and notification receipts are persisted in the GitHub-backed SQLite database so worker restarts do not reset the batch counter. Every folder is created with `public: true`.

Keep `GOFILE_API_TOKEN` private: anyone holding it has full access to the GoFile account. Do not commit it to GitHub or include it in logs.
