# Architecture

## Runtime

- Telegram Bot API: admin-only control, selection and status messages. No Telegram media upload or MTProto.
- Javtiful channel scraper: inspect channel totals, fetch selected pages and deduplicate video IDs.
- Selection helpers: one-based `all`, comma-separated indices and inclusive ranges.
- Download engine: Playwright stream resolver + yt-dlp, downloading into a unique temporary Colab job folder.
- Bounded queue: up to six download worker tasks behind an adjustable concurrency gate; a separate bounded upload queue and configured Drive upload workers.
- Google Drive manager: creates a root library and per-code folders, uploads files and maintains `index.txt`.
- JSON store: persistent completed catalogue and job states on mounted Drive.

## Job lifecycle

1. Receive a channel or video URL through Telegram.
2. Inspect the channel and ask which pages to scan.
3. Ask which discovered videos to queue; skip codes already in the library.
4. Ask for the active download concurrency (1–6).
5. Download into temporary Colab storage.
6. Enqueue the completed local file for a Drive upload worker.
7. Verify the uploaded byte size, record the catalogue code and refresh `index.txt`.
8. Delete the local job directory only after the upload and library update succeed.

A failed download is marked failed. A failed Drive upload is marked `upload_failed` and its local file is retained.

## Persistence and deployment

Mount Google Drive in Colab. `database.json` is stored inside the mounted library folder. The work directory is temporary Colab storage. This JSON store is designed for one active Colab runtime; do not run multiple workers against the same database.

## Current limitations

- Queue state is persisted, but automatic re-queue/recovery after a Colab runtime crash is not implemented.
- `/cancel` clears only the interactive selection state; it does not cancel already queued jobs.
- Per-job pause/resume/retry controls and scheduled channel scans are not implemented.
- The bot must run in the same Colab runtime where Drive is mounted and authenticated.
