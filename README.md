# TMJDLC — GoFile Edition

A lightweight Google Colab downloader controlled by a private Telegram bot. Videos are downloaded in the Colab runtime and uploaded to public GoFile folders. Telegram receives links only — video files are never uploaded to Telegram.

## Features

- Private Telegram Bot API controller (no MTProto upload client).
- Submit an individual Javtiful video URL or a channel URL.
- Channel inspection with all-videos, page-range, or individual-page selection (e.g. `all`, `pages 1-3`, `pages 1,3`).
- Concurrent pipeline: up to 2 downloads and 2 GoFile uploads at the same time.
- Live Telegram download progress with percentage, downloaded size, speed and ETA (where yt-dlp provides them).
- Public GoFile folders containing up to 20 successfully uploaded videos.
- Automatically reserves no more than 20 upload slots per folder, including concurrent uploads.
- Sends each completed 20-video folder link to the owner's private Telegram chat.
- `/status` reports current folder progress; `/finish` publishes a partial folder link.
- Local JSON state in `/content/TMJDLC/data/gofile_state.json`.
- No database, no queue, no duplicate detection.

## Run on Google Colab

<https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/gofile/notebooks/TMJDLC_GoFile.ipynb>

**[Open TMJDLC GoFile in Google Colab](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/gofile/notebooks/TMJDLC_GoFile.ipynb)**

1. Open the notebook using the link above.
2. Add `TG_BOT_TOKEN` and `TG_OWNER_ID` in Colab Secrets, then allow the notebook to access them.
3. Run the notebook cells in order and keep the final cell running.
4. Open your bot's private chat and send `/start`.
5. Send `/setgofilekey`, then send your GoFile API key in the next message. The bot verifies it and saves it in the local state file; no GoFile key is required in Colab Secrets.

## Telegram commands

- `/start` — help and controller instructions.
- `/status` — current folder number, video count and link.
- `/finish` — send the current partial folder link and close that folder.\n- `/setgofilekey` — configure or replace the GoFile API key through the private bot chat.

Send a Javtiful video URL as a normal message to process one video. For channel mode, send a URL like `https://javtiful.com/channel/NAME`; then reply `all`, `pages 1-3`, or `pages 1,3`. The bot runs up to two downloads and two GoFile uploads concurrently. Only the configured owner can control the bot.

## Folder behavior

- Each folder is public and named `TMJDLC Batch 001`, `TMJDLC Batch 002`, etc.
- A folder is announced after its 20th successful upload.
- The next folder is created lazily when the next video is submitted.
- Use `/finish` to announce a folder containing 1–19 videos.
- Empty folders are not announced.

## Local state

The app writes the GoFile API key, root folder, active folder, share code, successful/reserved counts, completed links and Telegram update offset to `data/gofile_state.json`. Keep this file private if you need to retain your GoFile account and current folder across notebook runs. Colab's temporary `/content` storage is cleared when the runtime is deleted; download this state file before a runtime reset if you need to preserve it.

Do not share the state file or commit it to GitHub: it contains your GoFile API key.

## Runtime architecture

`gofile_bot/` is the new controller/uploader implementation. It uses the existing Javtiful channel scraper, resolver and yt-dlp downloader. Two in-memory download workers feed two in-memory upload workers; there is no persistent queue, database or legacy MTProto bot.
