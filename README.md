# TMJDLC — Google Drive Edition

TMJDLC is a Colab-based Javtiful downloader with Google Drive as the permanent library. Telegram is used for control and progress only; videos are uploaded directly from Colab to Drive.

## Open in Google Colab

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/drive/notebooks/TMJDLC_Drive.ipynb)

## Pipeline

`Javtiful → temporary Colab storage → Google Drive → database.json + index.txt → local cleanup`

- Playwright-based stream detection, reusing the proven browser resolver.
- Javtiful channel pagination and page/video selection.
- Telegram Bot API inline concurrency selection (1–6 download workers).
- Separate bounded Google Drive upload workers.
- Duplicate detection using a persistent JSON catalogue on mounted Drive.
- Failed uploads retain their temporary file for recovery.

## Telegram bot

Configure `TG_BOT_TOKEN` and `TG_ADMIN_IDS`, mount Drive in Colab, then run:

```bash
python -m tmjdlc.bot
```

Send a channel URL to select pages and videos, or send a single video URL. Choose the download concurrency from the inline keyboard. Use `/status` to inspect the queue and `/cancel` to clear the current selection flow.

See [Colab setup](docs/SETUP.md) and [architecture](docs/ARCHITECTURE.md).

## Tests

```bash
python -m pytest -q
```

All code changes for this Google Drive edition belong on the `drive` branch.
