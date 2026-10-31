# TMJDLC

[![Open in Google Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Javtiful_Colab.ipynb)

## Run in Google Colab

Open the [Fresh Javtiful Downloader Colab notebook](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/main/notebooks/TMJDLC_Javtiful_Colab.ipynb) to set the post URL and quality, resolve the stream, download, validate the video, and generate screenshots.


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

## Player inspection

Use **Actions → Inspect Player → Run workflow**. The default input is the post URL from the supplied capture; replace it with another post URL when needed.

The workflow uses `requests` to:
- Fetch the post HTML.
- Save a sanitized HTML copy.
- Extract inline player configuration and playback endpoint candidates.
- Download referenced player JavaScript assets.
- Save script hashes, status codes and a JSON inspection report.

It does not call the playback endpoint or store the temporary playback ticket. Review the resulting artifact before implementing the playback request. Playwright remains a fallback for cases where the HTTP inspection does not expose the required behavior.

## Javtiful stream detection

The reusable Playwright detector is available as
`tmjdlc.providers.javtiful.JavtifulStreamDetector`. It opens a Javtiful post,
starts the player, captures the playback API response and resolves the selected
HLS, DASH or direct-media source. It prefers an explicitly requested quality;
otherwise it selects the highest quality returned by the player.

Example:

```python
from tmjdlc.providers.javtiful import JavtifulStreamDetector

detector = JavtifulStreamDetector(timeout_ms=45_000, capture_ms=20_000)
stream = detector.detect(
    "https://javtiful.com/video/113371/dvmm-444",
    preferred_quality=720,
)

# Pass the live URL and headers to the downloader in memory.
download_url = stream.stream_url
yt_dlp_options = stream.to_ytdlp_options()
ffmpeg_headers = stream.to_ffmpeg_headers()

# Safe for logs/job history; signed query values are redacted.
print(stream.to_public_dict())
```

The result retains the live signed URL and browser request headers in memory for
the downloader. Do not log `stream.stream_url`, `stream.headers`, or the
FFmpeg header string. Use `to_public_dict()` for diagnostics and persistent
job metadata. The detector does not download media itself; it provides a
download target to the existing/future download worker.

The resolved result can be passed directly to the yt-dlp adapter:

```python
from tmjdlc.downloaders import YtDlpStreamDownloader
from tmjdlc.providers.javtiful import JavtifulStreamDetector

stream = JavtifulStreamDetector().detect(
    "https://javtiful.com/video/113371/dvmm-444",
    preferred_quality=720,
)
file_path = YtDlpStreamDownloader().download(
    stream,
    output_dir="/content/tmjdlc-downloads",
    filename="DVMM-444",
    concurrent_fragments=4,
)
print(file_path)
```

The adapter passes the live stream URL and its Referer/User-Agent (and any
matching browser cookies) directly to yt-dlp in memory, supports fragment
retries and bounded parallel fragment downloads, and never prints the signed
URL in its own exception message. The caller should still keep progress-hook
payloads and browser headers out of persistent logs.
