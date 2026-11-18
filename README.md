# TMJDLC — Google Drive Edition

A clean rebuild of TMJDLC for Google Colab. The pipeline is:

**Javtiful discovery → temporary Colab download → Google Drive upload → remote verification → JSON library update → local cleanup.**

Telegram uses the Bot API for commands and progress notifications only. Video files are stored in Google Drive, not uploaded to Telegram.

## Current status

This commit establishes the new project structure and architecture. Downloader, provider resolver, Drive uploader, queue workers, and Telegram handlers will be implemented and tested in separate steps. This scaffold is not a runnable bot yet.

## Fixed design decisions

- Google Colab is the worker runtime.
- Colab local storage is temporary only; successful uploads are verified before local files are deleted.
- Google Drive is the permanent media destination.
- Telegram Bot API is used only for control and progress.
- `database.json` is the duplicate-detection and job-state index.
- Each video code gets a Drive folder; `index.txt` records entries such as `ABP {FOLDER LINK}`.
- Channel scanning supports page selection and duplicate filtering.
- Download concurrency and Drive upload concurrency are separate settings.
- No Neon/Postgres, GoFile, MTProto/WZGram, Telegram media uploads, or file splitting.

## Repository layout

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for module responsibilities and data flow, and [docs/SETUP.md](docs/SETUP.md) for the planned Colab setup.

## Development

Python 3.11+ is the target. Install with `pip install -r requirements.txt && pip install -e .`; run tests with `pytest`.

The implementation will keep provider, download, Drive, persistence, workers, and Telegram control code isolated so each stage can be tested independently.
