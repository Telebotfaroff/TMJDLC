from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from tmjdlc.models.stream import StreamResult

MEDIA_SUFFIXES = (".m3u8", ".mpd", ".mp4", ".m4v", ".webm")


class StreamDetectionError(RuntimeError):
    """Raised when a playable media source cannot be resolved."""


class JavtifulStreamDetector:
    """Open a Javtiful post, start playback and capture its actual media source."""

    def __init__(
        self,
        *,
        timeout_ms: int = 45_000,
        capture_ms: int = 30_000,
        headless: bool = True,
        user_agent: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.capture_ms = capture_ms
        self.headless = headless
        self.progress_callback = progress_callback
        self.user_agent = user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

    @staticmethod
    def _choose_source(
        sources: list[dict[str, Any]],
        preferred_quality: int | None,
        base_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Choose a playback source and normalize provider-relative media URLs."""
        valid: list[dict[str, Any]] = []
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("src"), str):
                continue
            raw_url = source["src"].strip()
            resolved_url = urljoin(base_url or "", raw_url)
            if urlsplit(resolved_url).scheme not in {"http", "https"}:
                continue
            normalized = dict(source)
            normalized["src"] = resolved_url
            fallback = normalized.get("fallbackSrc")
            if isinstance(fallback, str) and fallback.strip():
                normalized["fallbackSrc"] = urljoin(base_url or "", fallback.strip())
            valid.append(normalized)
        if not valid:
            return None
        if preferred_quality is not None:
            exact = [
                item for item in valid
                if str(item.get("size", "")).isdigit()
                and int(item["size"]) == preferred_quality
            ]
            if exact:
                return exact[0]

        def source_quality(item: dict[str, Any]) -> int:
            value = item.get("size")
            try:
                return int(value)
            except (TypeError, ValueError):
                digits = "".join(character for character in str(value or "") if character.isdigit())
                return int(digits or 0)

        return max(valid, key=source_quality)

    @staticmethod
    def _safe_url(url: str) -> str:
        """Return URL host/path only; never expose temporary signed query values."""
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def _progress(self, message: str) -> None:
        if self.progress_callback is not None:
            try:
                self.progress_callback(message)
            except Exception:
                # Progress reporting must never break stream detection.
                pass

    def detect(
        self,
        post_url: str,
        *,
        preferred_quality: int | None = None,
    ) -> StreamResult:
        """Use the exact CLI detector used by the GitHub Actions workflow.

        The browser interaction, network capture and media classification live in
        scripts/detect_stream.py. Colab and Actions therefore share one detector.
        """
        parsed = urlsplit(post_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("post_url must be an absolute HTTP or HTTPS URL")
        if parsed.hostname.lower() not in {"javtiful.com", "www.javtiful.com"}:
            raise ValueError("JavtifulStreamDetector only accepts javtiful.com post URLs")

        repo_root = Path(__file__).resolve().parents[3]
        detector_script = repo_root / "scripts" / "detect_stream.py"
        if not detector_script.is_file():
            raise StreamDetectionError(
                f"Shared Actions detector was not found at {detector_script}."
            )

        self._progress("Using the same scripts/detect_stream.py detector as GitHub Actions.")
        with tempfile.TemporaryDirectory(prefix="tmjdlc-javtiful-") as temp_dir:
            temp_root = Path(temp_dir)
            report_dir = temp_root / "report"
            resolved_path = temp_root / "resolved-stream.json"
            command = [
                sys.executable,
                str(detector_script),
                "--url",
                post_url,
                "--output",
                str(report_dir),
                "--timeout",
                str(max(1, self.timeout_ms // 1000)),
                "--capture-seconds",
                str(max(1, self.capture_ms // 1000)),
                "--resolved-output",
                str(resolved_path),
            ]
            if preferred_quality is not None:
                command.extend(["--quality", str(preferred_quality)])

            try:
                completed = subprocess.run(
                    command,
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=max(
                        60,
                        self.timeout_ms / 1000 + self.capture_ms / 1000 + 45,
                    ),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise StreamDetectionError(
                    "The shared Actions detector exceeded its browser timeout."
                ) from exc

            if completed.stdout:
                for line in completed.stdout.splitlines():
                    self._progress(f"[Actions detector] {line[:1200]}")
            if completed.returncode != 0 or not resolved_path.is_file():
                details = (completed.stderr or completed.stdout or "").strip()
                details = details[-5000:] if details else "No detector diagnostics were emitted."
                raise StreamDetectionError(
                    f"The shared Actions detector did not resolve a media URL "
                    f"(exit={completed.returncode}). {details}"
                )

            try:
                payload = json.loads(resolved_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StreamDetectionError(
                    "The shared Actions detector produced an invalid private stream result."
                ) from exc

        stream_url = payload.get("url")
        if not isinstance(stream_url, str) or urlsplit(stream_url).scheme not in {"http", "https"}:
            raise StreamDetectionError(
                "The shared Actions detector result did not contain a valid HTTP(S) media URL."
            )

        content_type = str(payload.get("content_type") or "").lower()
        path = urlsplit(stream_url).path.lower()
        if path.endswith(".m3u8") or "mpegurl" in content_type:
            media_format = "hls"
        elif path.endswith(".mpd") or "dash" in content_type:
            media_format = "dash"
        elif path.endswith((".mp4", ".m4v")) or "video/mp4" in content_type:
            media_format = "mp4"
        elif path.endswith(".webm") or "video/webm" in content_type:
            media_format = "mp4"
        else:
            raise StreamDetectionError(
                f"Unsupported media type returned by the shared detector: {content_type or path}."
            )

        quality_value = payload.get("quality")
        try:
            numeric_quality = int(quality_value) if quality_value is not None else None
        except (TypeError, ValueError):
            numeric_quality = None
        label = f"{numeric_quality}p" if numeric_quality else None

        user_agent = str(payload.get("user_agent") or self.user_agent)
        headers = {
            "Referer": post_url,
            "User-Agent": user_agent,
        }
        duration = payload.get("duration_seconds")
        try:
            duration_value = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_value = None

        result = StreamResult(
            post_url=post_url,
            stream_url=stream_url,
            format=media_format,
            quality=numeric_quality,
            label=label,
            fallback_url=None,
            duration_seconds=duration_value,
            headers=headers,
            detected_from="actions_workflow",
        )
        self._progress(
            f"Shared Actions detector resolved {media_format} "
            f"({label or 'quality unknown'}), HTTP {payload.get('status') or 'unknown'}."
        )
        return result
