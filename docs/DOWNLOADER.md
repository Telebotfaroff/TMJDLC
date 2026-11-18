# Downloader engine

The downloader is a one-job execution component; the queue/worker layer controls parallelism.

## Usage

```python
from tmjdlc.downloader import DownloadEngine

engine = DownloadEngine(
    "/content/TMJDLC-temp",
    progress_callback=lambda update: print(update),
)
result = engine.download("https://javtiful.com/video/ABP-123")
print(result.code, result.filepath, result.size)
```

Each job gets a unique directory and output filename. yt-dlp handles extraction, retrying transient network and fragment errors, fragmented streams, and merging audio/video to MP4 when possible. No splitting is performed.

DownloadResult contains the normalized code, title, local path, byte size, duration, thumbnail URL, source page URL, and extractor name. The caller uploads the result to Drive, records it only after remote verification, and then calls cleanup_job.

Call engine.cancel(job_id) from another thread while a download is active. The cancellation event is checked by yt-dlp progress hooks. Failed/cancelled job directories are retained for inspection or retry.

The provider accepts Javtiful page URLs and extracts conventional catalogue identifiers such as ABP-123. yt-dlp handles media extraction. Site markup and extractor support can change; validate a real page in Colab before enabling automatic channel scans.
