from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from tmjdlc.models.stream import StreamResult

MEDIA_SUFFIXES = (".m3u8", ".mpd", ".mp4", ".m4v", ".webm")


class StreamDetectionError(RuntimeError):
    """Raised when a playable media source cannot be resolved."""


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
        # Jupyter/Colab keeps an asyncio event loop running in the notebook thread.
        # Playwright's synchronous API cannot run there, so transparently move the
        # complete synchronous detection operation to a worker thread when needed.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(
                    self.detect,
                    post_url,
                    preferred_quality=preferred_quality,
                ).result()

        parsed = urlsplit(post_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("post_url must be an absolute HTTP or HTTPS URL")
        if parsed.hostname.lower() not in {"javtiful.com", "www.javtiful.com"}:
            raise ValueError("JavtifulStreamDetector only accepts javtiful.com post URLs")

        playback_sources: list[dict[str, Any]] = []
        network_sources: dict[str, str] = {}
        api_events: list[dict[str, Any]] = []
        failed_requests: list[dict[str, str]] = []
        duration: float | None = None
        navigation_error: str | None = None
        page_errors: list[str] = []
        page_diagnostics: dict[str, Any] = {}
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

                def collect_source_lists(value: Any) -> None:
                    """Find source arrays even when the API wraps them in data/result objects."""
                    if isinstance(value, dict):
                        sources = value.get("sources")
                        if isinstance(sources, list):
                            playback_sources.extend(
                                item for item in sources
                                if isinstance(item, dict)
                                and isinstance(item.get("src"), str)
                            )
                        for child in value.values():
                            if isinstance(child, (dict, list)):
                                collect_source_lists(child)
                    elif isinstance(value, list):
                        for child in value:
                            if isinstance(child, (dict, list)):
                                collect_source_lists(child)

                def on_request(request: Any) -> None:
                    if request.resource_type in {"xhr", "fetch"}:
                        api_events.append({
                            "phase": "request",
                            "method": request.method,
                            "resource_type": request.resource_type,
                            "url": self._safe_url(request.url),
                        })

                def on_request_failed(request: Any) -> None:
                    failed_requests.append({
                        "resource_type": request.resource_type,
                        "url": self._safe_url(request.url),
                        "failure": str(request.failure or "unknown")[:200],
                    })

                def on_response(response: Any) -> None:
                    url = response.url
                    content_type = response.headers.get("content-type", "")
                    if _is_media_url(url, content_type):
                        network_sources[url] = content_type
                    if response.request.resource_type in {"xhr", "fetch"}:
                        api_events.append({
                            "phase": "response",
                            "method": response.request.method,
                            "resource_type": response.request.resource_type,
                            "url": self._safe_url(url),
                            "status": response.status,
                            "content_type": content_type[:100],
                        })
                    if "json" in content_type.lower():
                        try:
                            collect_source_lists(response.json())
                        except Exception:
                            pass

                page.on("request", on_request)
                page.on("requestfailed", on_request_failed)
                page.on("response", on_response)
                page.on("pageerror", lambda error: page_errors.append(str(error)[:300]))
                page.on("console", lambda message: page_errors.append(
                    f"console.{message.type}: {message.text[:250]}"
                ) if message.type == "error" else None)

                try:
                    self._progress("Opening post page...")
                    page.goto(
                        post_url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    page.wait_for_timeout(2_500)
                    self._progress("Post page loaded; player scripts initialized.")
                except PlaywrightTimeoutError as exc:
                    navigation_error = type(exc).__name__

                self._progress("Starting the video player...")
                for selector in (
                    ".plyr__control--overlaid",
                    "button[aria-label*='play' i]",
                    "button[title*='play' i]",
                    "#front-player",
                    "video",
                ):
                    try:
                        control = page.locator(selector).first
                        if control.count() and control.is_visible():
                            control.click(
                                timeout=2_500,
                                force=selector in {"#front-player", "video"},
                            )
                            self._progress(f"Clicked player control: {selector}")
                            page.wait_for_timeout(1_500)
                            break
                    except Exception:
                        continue

                try:
                    page.evaluate(
                        """async () => {
                            const video = document.querySelector('#front-player video, video');
                            if (video) {
                                video.muted = true;
                                try {
                                    const pending = video.play();
                                    if (pending && pending.catch) pending.catch(() => {});
                                } catch (_) {}
                            }
                        }"""
                    )
                except Exception:
                    pass

                self._progress(
                    f"Capturing playback requests for {max(1_000, self.capture_ms) // 1000}s..."
                )
                capture_deadline = time.monotonic() + max(1_000, self.capture_ms) / 1000
                while time.monotonic() < capture_deadline and not playback_sources:
                    remaining_ms = int((capture_deadline - time.monotonic()) * 1000)
                    page.wait_for_timeout(max(100, min(250, remaining_ms)))
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
                try:
                    page_diagnostics = page.evaluate(
                        """() => ({
                            title: document.title || null,
                            body_text: (document.body?.innerText || '').slice(0, 700),
                            video_elements: [...document.querySelectorAll('video')].map(v => ({
                                src: v.getAttribute('src'),
                                current_src: v.currentSrc || null,
                                ready_state: v.readyState,
                                network_state: v.networkState,
                                paused: v.paused,
                                duration: Number.isFinite(v.duration) ? v.duration : null,
                                error_code: v.error?.code ?? null
                            })),
                            iframes: [...document.querySelectorAll('iframe')].map(f => f.src || f.getAttribute('src')).filter(Boolean).slice(0, 20),
                            player_scripts: [...document.scripts].map(s => s.src).filter(Boolean).slice(0, 40),
                            resource_hints: performance.getEntriesByType('resource')
                                .map(e => e.name)
                                .filter(u => /api|playback|player|video|stream|source|playlist|\.m3u8|\.mpd|\.mp4/i.test(u))
                                .slice(-80)
                        })"""
                    )
                except Exception as exc:
                    page_diagnostics = {"diagnostic_error": type(exc).__name__}
                browser_cookies = context.cookies()
                context.close()
            finally:
                browser.close()

        self._progress(
            f"Captured {len(playback_sources)} playback source entries and "
            f"{len(network_sources)} media network candidates."
        )
        source = self._choose_source(playback_sources, preferred_quality, final_url)
        if source is not None:
            src_parts = urlsplit(source["src"])
            fallback_value = source.get("fallbackSrc")
            fallback_parts = urlsplit(fallback_value) if isinstance(fallback_value, str) else None
            source_path_name = src_parts.path.rsplit("/", 1)[-1]
            source_path_suffix = (
                source_path_name.rsplit(".", 1)[-1] if "." in source_path_name else "none"
            )
            self._progress(
                "Selected playback source metadata: "
                f"keys={sorted(source.keys())}, "
                f"host={src_parts.hostname or 'unknown'}, "
                f"path_suffix={source_path_suffix}, "
                f"type={source.get('type')!r}, size={source.get('size')!r}, "
                f"label={source.get('label')!r}, "
                f"fallback_present={bool(fallback_value)}, "
                f"fallback_host={fallback_parts.hostname if fallback_parts else 'none'}"
            )
        detected_from = "playback_api"
        if source is None:
            candidates = [
                url for url in network_sources
                if urlsplit(url).path.lower().endswith(MEDIA_SUFFIXES)
            ]
            if not candidates:
                diagnostic_summary = {
                    "page": {
                        "title": page_diagnostics.get("title"),
                        "body_text": page_diagnostics.get("body_text"),
                        "video_elements": page_diagnostics.get("video_elements", []),
                        "iframes": [
                            self._safe_url(item) for item in page_diagnostics.get("iframes", [])
                        ],
                        "player_scripts": [
                            self._safe_url(item) for item in page_diagnostics.get("player_scripts", [])
                        ],
                        "resource_hints": [
                            self._safe_url(item) for item in page_diagnostics.get("resource_hints", [])
                        ],
                    },
                    "api_events": api_events[-35:],
                    "failed_requests": failed_requests[-15:],
                    "page_errors": page_errors[-10:],
                    "navigation_error": navigation_error,
                }
                self._progress(
                    "No stream found. Diagnostic summary: "
                    + str(diagnostic_summary)[:5000]
                )
                raise StreamDetectionError(
                    f"No HLS, DASH or direct media source detected for {self._safe_url(final_url)}. "
                    f"Diagnostics: {str(diagnostic_summary)[:5000]}"
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

        try:
            numeric_quality = int(quality) if quality is not None else None
        except (TypeError, ValueError):
            digits = "".join(
                character for character in str(quality or "") if character.isdigit()
            )
            numeric_quality = int(digits) if digits else None

        result = StreamResult(
            post_url=final_url,
            stream_url=stream_url,
            format=media_format,
            quality=numeric_quality,
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
