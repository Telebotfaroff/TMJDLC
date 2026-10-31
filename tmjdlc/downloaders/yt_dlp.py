from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tmjdlc.providers.javtiful.stream_detector import StreamResult


class YtDlpStreamDownloader:
    """Download a resolved HLS/DASH/MP4 source using yt-dlp."""

    @staticmethod
    def build_options(
        stream: StreamResult,
        output_dir: str | Path,
        filename: str,
        *,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
        concurrent_fragments: int = 4,
    ) -> dict[str, Any]:
        if stream.format not in {"hls", "dash", "mp4"}:
            raise ValueError(f"Unsupported stream format: {stream.format}")
        if not filename or Path(filename).name != filename:
            raise ValueError("filename must be a plain file name without directories")

        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        hooks = [progress_hook] if progress_hook else []
        return {
            "format": "best",
            "outtmpl": str(destination / f"{filename}.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": dict(stream.headers),
            "retries": 5,
            "fragment_retries": 5,
            "file_access_retries": 3,
            "continuedl": True,
            "overwrites": False,
            "concurrent_fragment_downloads": max(1, concurrent_fragments),
            "merge_output_format": "mp4",
            "progress_hooks": hooks,
        }

    def download(
        self,
        stream: StreamResult,
        output_dir: str | Path,
        filename: str,
        *,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
        concurrent_fragments: int = 4,
    ) -> Path:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                "yt-dlp is required. Install it with pip install yt-dlp."
            ) from exc

        options = self.build_options(
            stream,
            output_dir,
            filename,
            progress_hook=progress_hook,
            concurrent_fragments=concurrent_fragments,
        )
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(stream.stream_url, download=True)
                prepared = Path(downloader.prepare_filename(info))
        except Exception as exc:
            # Avoid echoing signed source URLs from third-party exception text.
            raise RuntimeError(
                f"Media download failed ({type(exc).__name__}). Check the worker log."
            ) from None

        merged = prepared.with_suffix(".mp4")
        if merged.exists():
            return merged
        if prepared.exists():
            return prepared
        candidates = sorted(Path(output_dir).glob(f"{filename}.*"))
        if candidates:
            return candidates[0]
        raise RuntimeError("yt-dlp completed without producing an output file.")
