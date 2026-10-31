# TMJDLC

A provider-based downloader and Telegram publishing project.

## Current implementation stage

The repository foundation is being established first. Website-specific selectors, metadata fields and streaming extraction are intentionally pending inspection of real page HTML.

## Scraping strategy

1. Try ordinary HTTP extraction with `requests`.
2. Check whether the response contains useful page content.
3. Fall back to Playwright only when the HTTP response appears blocked, incomplete or JavaScript-dependent.
4. Save both the original response and rendered page as GitHub Actions artifacts.
5. Implement provider selectors only after reviewing those artifacts.

## HTML investigation workflow

1. Open **Actions → Capture Page HTML**.
2. Select **Run workflow**.
3. Paste the exact studio, listing or post URL into `target_url`.
4. Run the workflow.
5. Download the `page-capture` artifact and share its HTML/network report for analysis.

The workflow records request/response metadata while redacting common secret query parameters. It does not save browser cookies or authorization headers.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Project decisions

- Runtime target: Google Colab
- Telegram integration: Pyrogram
- Temporary media storage: Colab local disk
- Persistent database: Neon PostgreSQL
- Studio processing: manual selection
- Scraping order: requests first, Playwright fallback
- Studio discovery: GitHub Actions

## Security

Never commit `.env`, Telegram session strings, API credentials or database connection strings.
