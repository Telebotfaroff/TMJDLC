# TMJDLC

[![Open TMJDLC Javtiful Telegram Bot in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb)

## Run the Javtiful Telegram bot in Google Colab

Open the [TMJDLC Telegram Bot notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb). Add `TG_BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH`, `TG_ADMIN_IDS`, and the Neon `DATABASE_URL` to Colab Secrets, then run the cells from top to bottom. The notebook pulls the latest `main` branch, installs TMJDLC/Chromium/FFmpeg, checks the Neon queue connection, and starts the bot using Pyrogram MTProto for all Telegram updates, commands, callbacks, messages, and large video/photo uploads.

Send a Javtiful `/channel/SLUG` URL to inspect it. Use `/batch CHANNEL_URL all` or `/batch CHANNEL_URL 1-5,8` to queue all pages or a custom page selection at 720p. Use `/setup` once to save your Telegram video and screenshot destination channel IDs in Neon. `/panel` shows queue and disk status; `/storage` shows Colab disk usage. `/cancel JOB_ID` requests cancellation of queued or active work. Job stages, completion receipts, and worker leases are stored in Neon so retries can resume from completed stages. Keep the Colab runtime running while jobs are processing; downloaded media is stored on its temporary local disk.

The worker renews its Neon lease every 30 seconds and pauses new downloads when free disk falls below `TMJDLC_MIN_FREE_DISK_GB` (default 3 GiB). After a job has uploaded its video and all screenshots successfully, automatic cleanup removes that job's local video, retry outputs, and screenshot directory; the Neon media row is then cleared of its temporary file path. Set `TMJDLC_AUTO_CLEAN=false` to disable this behavior. Telegram status messages show throttled download and Pyrogram upload progress (10% steps by default), plus completion and failure updates. Set `TMJDLC_NOTIFY_PROGRESS_STEP` to change the progress interval. Stage receipts prevent routine retries from re-uploading already recorded video and screenshot sends.

## Primary provider: MissAV

TMJDLC now uses the async [unofficial-api-for-missav](https://github.com/EchterAlsFake/unofficial-api-for-missav) package.

The integration lives in `tmjdlc.providers.missav.MissAVProvider` and provides:
- Async video metadata retrieval.
- Async search result iteration.
- Native HLS downloading with quality selection.
- A serializable metadata view that deliberately excludes media URLs.

Example:

```python
import asyncio
from tmjdlc.providers.missav import MissAVProvider

async def main():
    async with MissAVProvider() as provider:
        video = await provider.get_video("https://missav.ws/en/EXAMPLE")
        print(provider.metadata(video))
        await provider.download(video, "/content/tmjdlc-output", quality="best")

asyncio.run(main())
```

For Google Colab, use top-level `await` instead of `asyncio.run()`.

## Project architecture

- Runtime target: Google Colab
- Primary provider: MissAV async API
- Media download: provider's native HLS downloader
- Post-download validation and screenshots: FFprobe + FFmpeg
- Telegram integration: Pyrogram MTProto for updates and media uploads
- Temporary media storage: Colab local disk
- Persistent database: Neon PostgreSQL
- Scraping order: provider API first; generic HTML inspection only for diagnostics

## Javtiful provider

`tmjdlc.providers.javtiful.JavtifulProvider` invokes the same `scripts/detect_stream.py` browser detector used by GitHub Actions. It captures actual playback media requests, selects the requested quality when available, and refreshes the temporary URL on a failed download attempt. When playback does not initialize, the detector reports player controls, JavaScript errors, playback API events, and video element state to help compare Colab with Actions. Signed URLs, including Cloudflare R2 signature parameters, are redacted from public diagnostics. The existing MissAV provider remains a separate integration.

## HTML investigation workflow

For unrelated provider investigations, use **Actions → Capture Page HTML**. The workflow records request/response metadata while redacting common secret query parameters. It does not save browser cookies or authorization headers.

## Local setup

Requires Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

FFmpeg and FFprobe are required for media validation and screenshots.

## Security

Never commit `.env`, Telegram session strings, API credentials or database connection strings. Do not log or persist temporary media URLs, cookies, or authorization headers.

## Dependency license

The MissAV API dependency is licensed under AGPL-3.0-only. Review its licensing requirements before distributing or hosting an integrated application.
