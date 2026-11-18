# Architecture

## Pipeline

1. Telegram receives a single URL or a channel URL.
2. The Javtiful provider inspects the post/channel and returns normalized video metadata.
3. The queue rejects codes already present in the library or already queued.
4. A bounded download worker downloads into a per-job directory under Colab temporary storage.
5. A bounded Drive worker uploads the completed file into its code folder.
6. The Drive client verifies the remote file (at minimum file ID and size).
7. The library store atomically records the completed code, Drive folder URL, file metadata, and job result.
8. The index writer updates the root Drive `index.txt`.
9. Only after verification and durable state update does cleanup remove the local file.

## Packages

- `bot/`: Telegram Bot API commands, callbacks, and progress panel. No media upload code.
- `providers/javtiful/`: Javtiful page/channel parsing and stream resolution.
- `downloader/`: yt-dlp invocation, progress callbacks, and media validation.
- `drive/`: authenticated Drive API client, folder management, resumable upload, verification, and index writing.
- `queue/`: job model, bounded scheduling, retries, cancellation, and recovery.
- `storage/`: atomic JSON state and duplicate lookup.
- `workers/`: download/upload orchestration and lifecycle.
- `utils/`: configuration, logging, paths, and formatting helpers.

## Persistence

`database.json` is the local library/job-state file stored on mounted Drive, not ephemeral Colab storage. Writes must be atomic and protected against concurrent worker updates. A code is completed only after Drive verification succeeds.

The root Drive `index.txt` contains one line per code, for example:

`ABP {https://drive.google.com/drive/folders/FOLDER_ID}`

## Concurrency

Download and upload limits are independent. Downloads may continue while previous files upload. The upload queue is bounded to prevent Colab disk exhaustion. The scheduler applies backpressure before starting more downloads when temporary disk space is low.

## Failure rules

- Download failure: retain diagnostic state; do not create a completed-library entry.
- Upload failure: keep the local file for retry; do not mark completed.
- Verification failure: treat upload as failed; do not delete the local file.
- Successful verified upload: persist state and index, then delete the local file.
- Restart: recover interrupted jobs from JSON state and reconcile incomplete uploads where possible.

## Explicit exclusions

No Neon/Postgres, GoFile, MTProto/WZGram, Telegram video uploads, or splitting files into parts.
