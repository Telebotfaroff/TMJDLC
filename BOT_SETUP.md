# TMJDLC Telegram Bot

The first bot implementation is an admin-only Pyrogram controller for the existing Javtiful provider.

## Required environment

Create a `.env` file (never commit it):

```env
TG_API_ID=123456
TG_API_HASH=your_api_hash
TG_BOT_TOKEN=123456:your_bot_token
TG_ADMIN_IDS=123456789
TMJDLC_MAX_PARALLEL=2
TMJDLC_WORK_DIR=/content/tmjdlc-bot
```

Add the bot to both destination channels as an administrator with permission to post. Add it to the source channel (or use a channel it can already read).

## Install and run in Colab

```bash
!pip install -e .
!sudo apt-get update -qq && sudo apt-get install -y ffmpeg
!python -m playwright install --with-deps chromium
!python -m tmjdlc.bot
```

## Commands

- `/dl POST_URL [720]` — download one Javtiful post.
- `/channel @sourcechannel [limit]` — scan recent channel captions/text for Javtiful post URLs and queue unique posts.
- `/setupload CHANNEL_ID` — configure the video destination.
- `/setimages CHANNEL_ID` — configure the screenshot destination.
- `/settings` — show current destinations and parallel limit.
- `/status` — show job counts.

Each completed video generates ten evenly spaced FFmpeg screenshots. Every screenshot is sent as a separate photo with its own caption containing the video ID and screenshot number. The video is sent to the configured upload channel.

## Current implementation notes

- Only IDs in `TG_ADMIN_IDS` can invoke commands.
- Downloads use the existing `JavtifulProvider` and its fresh-resolution retry behavior.
- `TMJDLC_MAX_PARALLEL` controls concurrent downloads (1–4).
- Duplicate suppression uses a local SQLite database. In Colab, that database is lost when the runtime is deleted; move it to persistent storage or the project's Neon database before treating it as durable production history.
- The bot must be able to read the source channel and post to both destination channels.
- The bot process must remain running in the Colab session to receive Telegram updates.
