# TMJDLC

[![Open MissAV Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_MissAV_Colab.ipynb)

## Run in Google Colab

Open the [TMJDLC MissAV Colab notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_MissAV_Colab.ipynb). It installs the MissAV API, fetches metadata, downloads using the API's native asynchronous HLS downloader, validates the result with FFprobe, and generates screenshots.

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

## Javtiful status

Javtiful is retired from the active Colab workflow. Its old detector and smoke-test notebook are retained temporarily for reference and rollback; they are not used by the MissAV provider.

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
