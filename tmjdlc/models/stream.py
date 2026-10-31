from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_QUERY_KEYS = {
    "auth", "authorization", "key", "sig", "signature", "token", "ticket", "verify",
    "x-amz-credential", "x-amz-signature", "x-amz-security-token",
}


def redact_stream_url(url: str) -> str:
    """Mask signed query values while preserving non-sensitive query parameters."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    safe_query = [
        (key, "[REDACTED]" if key.lower() in SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


@dataclass(slots=True)
class StreamResult:
    """A stream URL and the in-memory request context needed by a downloader."""

    post_url: str
    stream_url: str
    format: str
    quality: int | None
    label: str | None
    fallback_url: str | None
    duration_seconds: float | None
    headers: dict[str, str] = field(default_factory=dict)
    detected_from: str = "network"

    @property
    def safe_stream_url(self) -> str:
        return redact_stream_url(self.stream_url)

    @property
    def safe_fallback_url(self) -> str | None:
        return redact_stream_url(self.fallback_url) if self.fallback_url else None

    def to_ytdlp_options(self) -> dict[str, Any]:
        """Return yt-dlp options without exposing headers in logs or reports."""
        return {"http_headers": dict(self.headers)}

    def to_ffmpeg_headers(self) -> str:
        """Return FFmpeg's CRLF-delimited -headers value."""
        return "".join(f"{key}: {value}\\r\\n" for key, value in self.headers.items())

    def to_public_dict(self) -> dict[str, Any]:
        """Safe metadata for logs, JSON reports and job history."""
        return {
            "post_url": self.post_url,
            "stream_url": self.safe_stream_url,
            "format": self.format,
            "quality": self.quality,
            "label": self.label,
            "fallback_url": self.safe_fallback_url,
            "duration_seconds": self.duration_seconds,
            "detected_from": self.detected_from,
        }
