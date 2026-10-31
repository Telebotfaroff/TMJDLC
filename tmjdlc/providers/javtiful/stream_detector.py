from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

MEDIA_SUFFIXES = (".m3u8", ".mpd", ".mp4", ".m4v", ".webm")
SENSITIVE_QUERY_KEYS = {
    "auth", "authorization", "key", "sig", "signature", "token", "ticket", "verify",
}


class StreamDetectionError(RuntimeError):
    """Raised when a Javtiful page does not expose a usable media source."""


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


def _is_media_url(url: str, content_type: str = "") -> bool:
    path = urlsplit(url).path.lower()
    mime = content_type.lower().split(";", 1)[0].strip()
    return path.endswith(MEDIA_SUFFIXES) or mime in {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "application/dash+xml",
        "video/mp4",
        "video/webm",
    }


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
        return "".join(f"{key}: {value}\r\n" for key, value in self.headers.items())

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


class JavtifulStreamDetector:
    """Open a Javtiful post, start playback and capture its actual media source."""

    def __init__(
        self,
        *,
        timeout_ms: int = 45_000,
        capture_ms: int = 20_000,
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
        sources: list[dict[str, Any]], preferred_quality: int | None
    ) -> dict[str, Any] | None:
        valid = [
            source
            for source in sources
            if isinstance(source, dict)
            and isinstance(source.get("src"), str)
            and source["src"].startswith(("http://", "https://"))
        ]
        if not valid:
            return None
        if preferred_quality is not None:
            exact = [item for item in valid if item.get("size") == preferred_quality]
            if exact:
                return exact[0]
        return max(
            valid,
            key=lambda item: int(item.get("size") or 0),
        )

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
        parsed = urlsplit(post_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("post_url must be an absolute HTTP or HTTPS URL")
        if parsed.hostname.lower() not in {"javtiful.com", "www.javtiful.com"}:
            raise ValueError("JavtifulStreamDetector only accepts javtiful.com post URLs")

        playback_sources: list[dict[str, Any]] = []
        network_sources: dict[str, str] = {}
        duration: float | None = None
        navigation_error: str | None = None
        page_errors: list[str] = []
        final_url = post_url
        request_headers: dict[str, str] = {
            "Referer": post_url,
            "User-Agent": self.user_agent,
        }
        browser_cookies: list[dict[str, Any]] = []

        self._progress("Starting Playwright...")
        with sync_playwright() as playwright:
            self._progress("Launching Chromium...")
            browser = playwright.chromium.launch(
                headless=self.headless,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1365, "height": 900},
                    user_agent=self.user_agent,
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)

                def on_response(response: Any) -> None:
                    nonlocal duration
                    url = response.url
                    content_type = response.headers.get("content-type", "")
                    if _is_media_url(url, content_type):
                        network_sources[url] = content_type

                    if "playback" not in url.lower() or "json" not in content_type.lower():
                        return
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    if not isinstance(payload, dict) or not payload.get("success"):
                        return
                    sources = payload.get("sources")
                    if isinstance(sources, list):
                        playback_sources.extend(
                            source for source in sources if isinstance(source, dict)
                        )

                page.on("response", on_response)
                page.on("pageerror", lambda error: page_errors.append(str(error)[:300]))

                try:
                    self._progress("Opening post page...")
                    page.goto(
                        post_url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    page.wait_for_timeout(1_000)
                    self._progress("Post page loaded.")
                except PlaywrightTimeoutError as exc:
                    navigation_error = type(exc).__name__

                self._progress("Starting the video player...")
                for selector in (
                    ".plyr__control--overlaid",
                    "button[aria-label*='play' i]",
                    "button[title*='play' i]",
                ):
                    try:
                        control = page.locator(selector).first
                        if control.count() and control.is_visible():
                            control.click(timeout=2_500)
                            break
                    except Exception:
                        continue

                try:
                    page.evaluate(
                        """async () => {
                            const video = document.querySelector('#front-player video, video');
                            if (video) {
                                video.muted = true;
                                try { await video.play(); } catch (_) {}
                            }
                        }"""
                    )
                except Exception:
                    pass

                self._progress(
                    f"Capturing playback requests for {max(1_000, self.capture_ms) // 1000}s..."
                )
                page.wait_for_timeout(max(1_000, self.capture_ms))
                self._progress("Playback capture window finished; inspecting sources...")
                try:
                    state = page.evaluate(
                        """() => {
                            const video = document.querySelector('#front-player video, video');
                            return video && Number.isFinite(video.duration)
                                ? video.duration : null;
                        }"""
                    )
                    duration = float(state) if state is not None else None
                except Exception:
                    duration = None

                final_url = page.url
                browser_cookies = context.cookies()
                context.close()
            finally:
                browser.close()

        self._progress(
            f"Captured {len(playback_sources)} playback source entries and "
            f"{len(network_sources)} media network candidates."
        )
        source = self._choose_source(playback_sources, preferred_quality)
        detected_from = "playback_api"
        if source is None:
            candidates = [
                url for url in network_sources
                if urlsplit(url).path.lower().endswith(MEDIA_SUFFIXES)
            ]
            if not candidates:
                detail = f" Navigation: {navigation_error}." if navigation_error else ""
                raise StreamDetectionError(
                    f"No HLS, DASH or direct media source detected for {final_url}.{detail}"
                )
            stream_url = candidates[0]
            fallback_url = None
            quality = preferred_quality
            label = None
            mime = network_sources[stream_url]
            detected_from = "network"
        else:
            stream_url = source["src"]
            fallback_url = source.get("fallbackSrc")
            quality = source.get("size")
            label = source.get("label")
            mime = source.get("type", "")

        stream_host = (urlsplit(stream_url).hostname or "").lower()
        matching_cookies = []
        for cookie in browser_cookies:
            cookie_domain = str(cookie.get("domain", "")).lstrip(".").lower()
            if stream_host and (
                stream_host == cookie_domain or stream_host.endswith("." + cookie_domain)
            ):
                matching_cookies.append(f"{cookie['name']}={cookie['value']}")
        if matching_cookies:
            request_headers["Cookie"] = "; ".join(matching_cookies)

        path = urlsplit(stream_url).path.lower()
        if path.endswith(".m3u8") or "mpegurl" in str(mime).lower():
            media_format = "hls"
        elif path.endswith(".mpd") or "dash" in str(mime).lower():
            media_format = "dash"
        elif path.endswith((".mp4", ".m4v")) or "video/mp4" in str(mime).lower():
            media_format = "mp4"
        else:
            media_format = "unknown"

        result = StreamResult(
            post_url=final_url,
            stream_url=stream_url,
            format=media_format,
            quality=int(quality) if quality is not None else None,
            label=str(label) if label is not None else None,
            fallback_url=fallback_url if isinstance(fallback_url, str) else None,
            duration_seconds=duration,
            headers=request_headers,
            detected_from=detected_from,
        )
        self._progress(
            f"Stream resolved: {media_format}, quality={quality or 'unknown'}."
        )
        return result
