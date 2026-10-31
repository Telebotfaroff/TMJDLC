# TMJDLC

[![Open Download Test in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb)

## Test Javtiful downloads in Google Colab

Open the [TMJDLC Javtiful Download Test notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Download_Test.ipynb). It installs the repository and Chromium, accepts an original Javtiful page URL and quality, captures the playback API source, resolves relative media paths, and downloads with yt-dlp. Failed download attempts trigger a fresh playback resolution rather than reusing an expired signed URL. The resolver and downloader run in the same Colab runtime/network. The notebook displays only a redacted stream report and validates the completed file with FFprobe.

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
- Telegram integration: Pyrogram (pipeline integration remains separate)
- Temporary media storage: Colab local disk
- Persistent database: Neon PostgreSQL
- Scraping order: provider API first; generic HTML inspection only for diagnostics

## Javtiful provider

`tmjdlc.providers.javtiful.JavtifulProvider` resolves the original post URL immediately before downloading. It captures the playback API response, supports relative source URLs, selects the requested quality, and refreshes the temporary URL on a failed download attempt. Signed URLs, including Cloudflare R2 signature parameters, are redacted from public metadata. The existing MissAV provider remains a separate integration.

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
