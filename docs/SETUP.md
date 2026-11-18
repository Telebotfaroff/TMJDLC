# Colab setup

This foundation is ready for local tests. The full bot entry point and downloader are not implemented yet.

## Install

```bash
git clone -b drive https://github.com/Telebotfaroff/TMJDLC.git
cd TMJDLC
pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` and set:

- `TG_BOT_TOKEN`
- `TG_ADMIN_IDS` (comma-separated numeric Telegram IDs)
- `TMJDLC_WORK_DIR` (temporary Colab disk)
- `TMJDLC_DRIVE_ROOT_NAME` or an existing `TMJDLC_DRIVE_ROOT_ID`
- Download and upload worker limits

Mount Drive in Colab, then use `DriveManager.from_colab()` to authenticate and initialize the library. Store `database.json` inside the mounted library root, not in the temporary work directory.

## Tests

```bash
python -m pytest -q
```

The Drive API is isolated behind `DriveManager`; its live authentication and upload operations must be verified from the user's Colab runtime. Unit tests do not require Google credentials.


## Javtiful browser resolver

The downloader reuses the Playwright-based resolver from the original main branch.
After installing Python requirements, install Chromium in the Colab runtime:

```bash
python -m playwright install --with-deps chromium
```

The resolver launches Chromium headlessly, activates the Javtiful player, captures
the actual media request, and passes that short-lived URL directly to yt-dlp.
Do not print or persist the resolved signed media URL.
