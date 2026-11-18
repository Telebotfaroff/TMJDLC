# TMJDLC — Google Drive Edition

A clean rebuild of TMJDLC for Google Colab. The pipeline is:

**Javtiful discovery → temporary Colab download → Google Drive upload → remote verification → JSON library update → local cleanup.**

Telegram uses the Bot API for commands and progress notifications only. Video files are stored in Google Drive, not uploaded to Telegram.

## Current status

Implemented foundation: configuration, persistent JSON library, Google Drive folder/upload/index manager, Javtiful URL validation, browser-based stream resolution, yt-dlp download engine, and a tested channel pagination scraper. Telegram command handlers, interactive page/video selection, and queue orchestration are still pending. This is not yet a complete runnable bot.

## Fixed design decisions

- Google Colab is the worker runtime.
- Colab local storage is temporary only; successful uploads are verified before local files are deleted.
- Google Drive is the permanent media destination.
- Telegram Bot API is used only for control and progress.
- database.json is the duplicate-detection and job-state index.
- Each video code gets a Drive folder; index.txt records entries such as ABP {FOLDER LINK}.
- Download concurrency and Drive upload concurrency are separate settings.
- No Neon/Postgres, GoFile, MTProto/WZGram, Telegram media uploads, or file splitting.

## Channel scanner

The JavtifulChannelScraper validates a /channel/SLUG URL, reads the channel's video count, calculates pagination, and fetches only selected pages. It collapses duplicate video links by Javtiful's numeric video ID. Example:

```python
from tmjdlc.providers.javtiful import JavtifulChannelScraper

scraper = JavtifulChannelScraper()
info = scraper.inspect("https://javtiful.com/channel/CHANNEL_SLUG")
print(info.name, info.total_videos, info.total_pages)

# Fetch pages selected by the user:
video_urls = scraper.videos_for_pages(info, [1, 2, 5])
```

This is the scanner layer only; the Telegram page-selection interface and job queue will be connected in a later step.

## Downloader

See docs/DOWNLOADER.md for the one-job DownloadEngine API. It accepts Javtiful page URLs, uses the browser-based stream detector and yt-dlp, emits progress callbacks, retains failed job files, and returns a DownloadResult.

## Google Colab

[Open TMJDLC in Google Colab](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/drive/notebooks/TMJDLC_Drive.ipynb)

The notebook installs the drive branch, runs tests, mounts Google Drive, and provides a single-video Javtiful download → Drive upload workflow. Telegram bot controls and parallel queue orchestration are not yet included.

## Development

Python 3.11+ is the target. Install with pip install -r requirements.txt && pip install -e .; run tests with pytest. A real Javtiful channel page should be tested from Colab because page markup and pagination can change.
