# TMJDLC — Google Drive Edition

A clean rebuild of TMJDLC for Google Colab. The pipeline is:

**Javtiful discovery → temporary Colab download → Google Drive upload → remote verification → JSON library update → local cleanup.**

Telegram uses the Bot API for commands and progress notifications only. Video files are stored in Google Drive, not uploaded to Telegram.

## Current status

Implemented foundation: configuration, persistent JSON library, Google Drive folder/upload/index manager, Javtiful URL validation, browser-based stream resolution, yt-dlp download engine, channel pagination scraper, and reusable page/video selection with duplicate filtering. Telegram command handlers, interactive page/video selection, and queue orchestration are still pending. This is not yet a complete runnable bot.

## Fixed design decisions

- Google Colab is the worker runtime.
- Colab local storage is temporary only; successful uploads are verified before local files are deleted.
- Google Drive is the permanent media destination.
- Telegram Bot API is used only for control and progress.
- database.json is the duplicate-detection and job-state index.
- Each video code gets a Drive folder; index.txt records entries such as ABP {FOLDER LINK}.
- Download concurrency and Drive upload concurrency are separate settings.
- No Neon/Postgres, GoFile, MTProto/WZGram, Telegram media uploads, or file splitting.

## Channel scanning and selection

The JavtifulChannelScraper validates a /channel/SLUG URL, reads the channel's video count, calculates pagination, and fetches only selected pages. It collapses duplicate links by Javtiful's numeric video ID.

Use one-based page and video selections: all, 1,3,5-8. Selection helpers validate ranges, remove repeated video IDs, and skip catalogue codes already present in the persistent database.

```python
from tmjdlc.providers.javtiful import JavtifulChannelScraper, select_videos

scraper = JavtifulChannelScraper()
info = scraper.inspect("https://javtiful.com/channel/CHANNEL_SLUG")
print(info.name, info.total_videos, info.total_pages)
video_urls = scraper.videos_for_pages(info, [1, 2, 5])

selection = select_videos(
    video_urls,
    "1,3,5-8",
    completed_codes={"ABP-123"},  # use codes from JsonStore.read()["videos"]
)
for video in selection.selected:
    print(video.index, video.code, video.url)
print("Skipped duplicates:", len(selection.skipped_duplicates))
```

The selection layer is reusable by the future Telegram inline-keyboard interface. The Telegram page/video selection interface and download queue are not connected yet.

## Downloader

See docs/DOWNLOADER.md for the one-job DownloadEngine API. It uses the browser-based stream detector and yt-dlp, emits progress callbacks, retains failed job files, and returns a DownloadResult. The proven stream detector and downloader are kept separate from the new selection layer.

## Google Colab

[Open TMJDLC in Google Colab](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/drive/notebooks/TMJDLC_Drive.ipynb)

The notebook installs the drive branch, runs tests, mounts Google Drive, and provides a single-video Javtiful download → Drive upload workflow. Telegram bot controls and parallel queue orchestration are not yet included.

## Development

Python 3.11+ is the target. Install with pip install -r requirements.txt && pip install -e .; run tests with pytest. A real Javtiful channel page should be tested from Colab because page markup and pagination can change.
