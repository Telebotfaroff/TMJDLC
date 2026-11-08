# TMJDLC — GoFile Edition

A lightweight Google Colab downloader controlled by a private Telegram bot. Videos are downloaded in the Colab runtime and uploaded to public GoFile folders. Telegram receives links only — video files are never uploaded to Telegram.

## Features

- Private Telegram Bot API controller (no MTProto upload client).
- Submit one Javtiful video URL at a time.
- Direct sequential processing: download, upload, report.
- Public GoFile folders containing up to 20 successfully uploaded videos.
- Automatically starts a new folder when the next video arrives after a folder reaches 20.
- Sends each completed 20-video folder link to the owner's private Telegram chat.
- `/status` reports current folder progress; `/finish` publishes a partial folder link.
- Local JSON state in `/content/TMJDLC/data/gofile_state.json`.
- No database, no queue, no duplicate detection.

## Colab setup

Open [TMJDLC GoFile notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/gofile/notebooks/TMJDLC_GoFile.ipynb).

Add these values in Colab Secrets and allow notebook access:

- `TG_BOT_TOKEN` — BotFather token.
- `TG_OWNER_ID` — your numeric Telegram user ID.
- `GOFILE_API_TOKEN` — optional GoFile account token. If omitted, the app creates a guest account and saves its token in the local state file.

Run the notebook cells in order and keep the final cell running. Open the bot's private chat and send `/start`.

## Telegram commands

- `/start` — help and controller instructions.
- `/status` — current folder number, video count and link.
- `/finish` — send the current partial folder link and close that folder.

Send a Javtiful video URL as a normal message to start processing. Only the configured owner can control the bot.

## Folder behavior

- Each folder is public and named `TMJDLC Batch 001`, `TMJDLC Batch 002`, etc.
- A folder is announced after its 20th successful upload.
- The next folder is created lazily when the next video is submitted.
- Use `/finish` to announce a folder containing 1–19 videos.
- Empty folders are not announced.

## Local state

The app writes its GoFile account token, root folder, active folder, share code, count, completed links and Telegram update offset to `data/gofile_state.json`. Keep this file if you need to retain the guest account and current folder across notebook runs. Colab's temporary `/content` storage is cleared when the runtime is deleted; download this state file before a runtime reset if you need to preserve it.

Do not share the state file or commit it to GitHub: it contains the GoFile account token.

## Runtime architecture

`gofile_bot/` is the new controller/uploader implementation. It uses the existing Javtiful resolver and yt-dlp downloader only; it does not start the legacy MTProto bot, Neon integration or queue worker.
