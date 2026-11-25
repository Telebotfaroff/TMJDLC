# TMJDLC — Javtiful Telegram Downloader

[![Open TMJDLC in Google Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb)

TMJDLC is a Javtiful Telegram downloader built with Python, WZGram MTProto, yt-dlp, SQLite, and a private GitHub data repository. It downloads videos at **720p automatically**; there is no quality selector or provider selector.

## Telegram interface

Use `/start` to open the persistent menu:

- **Help** (`/help`) — show command and feature instructions.
- **Download Video** — send a Javtiful video URL; it is queued automatically at 720p.
- **Batch Download** — send a Javtiful channel URL, then choose new videos, all videos, latest videos, or custom pages.
- **Queue** — inspect recent jobs and use the existing cancellation/retry commands.
- **Settings / Upload Destination** — choose Telegram, GoFile, or both; GoFile reuses one persistent guest account and shared folder.
- **Settings** — configure the Telegram video destination, 1–2 download workers, 1–4 upload workers, and 0–2 waiting uploads per worker.
- **Storage** and **Worker Status** — inspect runtime health.
- **Live Processes** (`/processes`) — inspect active downloads, uploads, waiting upload backlog, worker IDs, progress, and free disk; use Refresh for an updated snapshot.
- **Channel Catalogue** (`/channels`) — scan Javtiful listing pages, save discovered channel URLs, and browse saved channels.
- **Custom Menu** (`/commands`) — hide/show, rename, and reorder Telegram menu buttons.

The original command shortcuts remain available: `/dl`, `/batch`, `/setup`, `/setupload`, `/panel`, `/queue`, `/settings`, `/commands`, `/channels`, `/processes`, `/storage`, `/cancel`, and `/retry`.

## Run in Google Colab

Open the [TMJDLC notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb). Set `GITHUB_TOKEN` either as a Colab Secret (enable notebook access) or as an environment variable injected by Google Colab CLI, then run the notebook cells.

The bot uses WZGram (Pyrogram-compatible MTProto client) for Telegram updates and media uploads. The upload client uses the bot token and WZGram's concurrent-transmission setting. Optional WZML-X-style helper bot tokens can distribute channel uploads across eligible bot clients; each helper bot must be an administrator in the configured video channel. A dedicated secondary upload-only bot can use its own API ID/hash and bot token via `TG_SECONDARY_API_ID`, `TG_SECONDARY_API_HASH`, and `TG_SECONDARY_BOT_TOKEN`. It shares the primary queue and upload workers but never downloads or handles control commands. Both primary and secondary bots must be administrators in the destination channel. The main bot remains responsible for private-chat fallback. A local SQLite queue provides transactional worker coordination; compressed database snapshots, configuration, channel catalogue, and completed upload records are stored in the private GitHub data repository. Keep the bot start cell running while jobs are processing. **Colab Free is not an always-on host:** closing the browser may leave the runtime running temporarily, but Google can disconnect or reclaim the session at any time; there is no supported way to guarantee that it stays active after the browser is closed. Do not stop or disconnect the runtime while jobs are active. If it disconnects, reopen the notebook and rerun the setup cells; GitHub-backed snapshots restore saved queue state, but media files on Colab's temporary disk may need to be downloaded again. A single active Colab runtime should write to the data repository at a time.

## Worker behavior

- Automatic 720p selection for single and batch downloads.
- Durable SQLite queue with retry and restart recovery, synchronized to GitHub snapshots.
- Separate download and upload progress reporting.
- One or two download workers and one to four upload workers, with a hard maximum of two downloads and four uploads.
- Per-upload-worker backlog limits (0, 1, or 2 waiting videos per worker) are configurable in Telegram Settings and prevent uncontrolled downloads from filling Colab storage.
- Live process monitor (`/processes`) shows active downloads, uploads, waiting uploads, worker IDs, and progress.
- Batch-scoped cancellation.
- Disk-space guard and automatic cleanup after successful upload.
- Javtiful stream resolution is refreshed before retrying a failed download.

## Configuration

Set only `GITHUB_TOKEN` in Colab. The token must have Contents: Read and write access to the private `gitroff/Database` repository. On first startup, TMJDLC creates the repository layout; add Telegram credentials to `config/secrets.json` there. Videos larger than 1,950,000,000 bytes are automatically split into independently playable MP4 parts below Telegram's 2 GB upload limit; parts are uploaded sequentially and tracked individually for restart recovery. Bot settings are persisted through the data repository. Optional `TG_HELPER_BOT_TOKENS` in the private repository's `config/secrets.json` accepts comma- or space-separated helper bot tokens. For one secondary upload-only client with separate Telegram application credentials, set `TG_SECONDARY_BOT_TOKEN`, `TG_SECONDARY_API_ID`, and `TG_SECONDARY_API_HASH` together. Add the secondary bot as an administrator in the destination channel with posting permission. Keep these fields empty to use only the primary upload account. The `config/commands.json` file stores the customizable menu layout. FFmpeg and FFprobe are required for media validation and video thumbnail generation.

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

Keep `gitroff/Database` private: `GOFILE_API_TOKEN` and Telegram credentials are stored in its `config/secrets.json`. Never copy those values into the public TMJDLC code repository or logs.
