"""Javtiful provider: resolve short-lived playback URLs immediately before download."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tmjdlc.downloaders.yt_dlp import YtDlpStreamDownloader
from tmjdlc.models.stream import StreamResult
from tmjdlc.providers.javtiful.stream_detector import JavtifulStreamDetector


def _safe_error_detail(error: Exception) -> str:
    """Keep useful diagnostics while stripping query strings from embedded URLs."""
    message = str(error).strip()
    message = re.sub(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]*", r"\1?[QUERY REDACTED]", message)
    return message[:600] or "No additional error details were provided."


class JavtifulProvider:
    """Resolve Javtiful playback sources and download them in the same runtime."""

    def __init__(
        self,
        *,
        detector: JavtifulStreamDetector | None = None,
        downloader: YtDlpStreamDownloader | None = None,
        max_attempts: int = 2,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.progress_callback = progress_callback
        self.detector = detector or JavtifulStreamDetector(
            progress_callback=self._progress
        )
        self.downloader = downloader or YtDlpStreamDownloader()
        self.max_attempts = max_attempts

    def _progress(self, message: str) -> None:
        if self.progress_callback is not None:
            try:
                self.progress_callback(message)
            except Exception:
                pass

    def resolve(
        self, post_url: str, *, quality: int | None = None
    ) -> StreamResult:
        """Resolve a fresh, temporary stream URL from the original post URL."""
        return self.detector.detect(post_url, preferred_quality=quality)

    def download(
        self,
        post_url: str,
        output_dir: str | Path,
        filename: str,
        *,
        quality: int | None = None,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
        concurrent_fragments: int = 8,
    ) -> tuple[Path, StreamResult]:
        """Resolve immediately before each download attempt.

        A failed attempt triggers a fresh browser resolution. Each retry uses a
        separate output stem so a partial file from an expired URL is not resumed
        against a newly signed URL.
        """
        if not filename or Path(filename).name != filename:
            raise ValueError("filename must be a plain file name without directories")

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                self._progress(
                    f"Resolving a fresh Javtiful stream (attempt {attempt}/{self.max_attempts})..."
                )
                stream = self.resolve(post_url, quality=quality)
                self._progress(
                    f"Resolved {stream.format} source "
                    f"({stream.label or stream.quality or 'quality unknown'}); starting download."
                )
                attempt_name = filename if attempt == 1 else f"{filename}.retry{attempt}"
                path = self.downloader.download(
                    stream,
                    output_dir,
                    attempt_name,
                    progress_hook=progress_hook,
                    concurrent_fragments=concurrent_fragments,
                )
                self._progress(f"Download completed: {path.name}")
                return path, stream
            except Exception as exc:
                last_error = exc
                detail = _safe_error_detail(exc)
                self._progress(
                    f"Attempt {attempt} failed ({type(exc).__name__}): {detail}"
                )
                if attempt < self.max_attempts:
                    self._progress("Resolving a new stream before retrying.")

        raise RuntimeError(
            f"Javtiful download failed after {self.max_attempts} fresh resolution attempt(s). "
            f"Last error: {type(last_error).__name__ if last_error else 'unknown'}: "
            f"{_safe_error_detail(last_error) if last_error else 'No details'}"
        ) from None
