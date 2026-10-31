"""Javtiful provider: resolve short-lived playback URLs immediately before download."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tmjdlc.downloaders.yt_dlp import YtDlpStreamDownloader
from tmjdlc.models.stream import StreamResult
from tmjdlc.providers.javtiful.stream_detector import JavtifulStreamDetector


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
            self._progress(
                f"Resolving a fresh Javtiful stream (attempt {attempt}/{self.max_attempts})..."
            )
            stream = self.resolve(post_url, quality=quality)
            self._progress(
                f"Resolved {stream.format} source ({stream.label or stream.quality or 'quality unknown'}); starting download."
            )
            attempt_name = filename if attempt == 1 else f"{filename}.retry{attempt}"
            try:
                path = self.downloader.download(
                    stream,
                    output_dir,
                    attempt_name,
                    progress_hook=progress_hook,
                )
                self._progress(f"Download completed: {path.name}")
                return path, stream
            except Exception as exc:
                last_error = exc
                self._progress(
                    f"Download attempt {attempt} failed ({type(exc).__name__})."
                )
                if attempt < self.max_attempts:
                    self._progress("Refreshing the temporary stream URL before retrying.")

        raise RuntimeError(
            f"Javtiful download failed after {self.max_attempts} fresh resolution attempt(s). "
            f"Last error: {type(last_error).__name__ if last_error else 'unknown'}"
        ) from None
