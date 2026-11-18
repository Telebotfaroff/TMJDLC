# Architecture

## Implemented foundation

- tmjdlc.config.Settings loads and validates environment configuration.
- tmjdlc.storage.JsonStore stores the durable library and job state in JSON. Writes use a temporary file, fsync, atomic replacement, thread/process locking, and a previous-version backup.
- tmjdlc.drive.DriveManager authenticates through Colab, creates/reuses the library root and per-code folders, uploads files resumably, verifies uploaded size, and maintains root index.txt.
- tmjdlc.providers.javtiful.JavtifulProvider validates Javtiful page URLs and extracts conventional catalogue codes.
- tmjdlc.downloader.DownloadEngine runs one yt-dlp download in a unique temporary job directory, reports progress, supports cancellation, and returns normalized media metadata.

## Intended job lifecycle

1. Receive a request through Telegram Bot API.
2. Normalize provider metadata and code.
3. Reject codes already in the JSON library or active queue.
4. Download into a unique temporary job directory.
5. Upload to that code's Google Drive folder.
6. Verify the Drive file ID and uploaded byte size.
7. Record the completed video in database.json and refresh index.txt.
8. Delete the local temporary file only after durable completion.

A failed upload must remain retryable and must not be recorded as completed. Failed downloads are retained for diagnosis/retry.

## Persistence

Mount Google Drive in Colab and place database.json under the mounted Drive library root. The work directory is temporary Colab storage. Do not keep the only copy of the database on the ephemeral runtime disk.

The JSON store exposes atomic updates and duplicate lookup. For this single Colab worker design, it uses local file locks; do not run multiple independent Colab runtimes against the same JSON database. If multi-runtime processing is introduced later, move coordination to a transactional database.

## Concurrency

DownloadEngine handles one job at a time. A future worker pool must enforce the configured download limit separately from Drive upload concurrency. The upload queue must be bounded so downloads cannot fill Colab storage while Drive is slow.

## Not included yet

Telegram command handlers, channel pagination/scanning, job queue orchestration, scheduled scans, and restart recovery. No Telegram media uploads, MTProto/WZGram, GoFile, Neon/Postgres, or video splitting.
