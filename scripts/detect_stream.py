from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

MEDIA_EXTENSIONS = (".m3u8", ".mpd", ".mp4", ".m4v", ".webm")
MEDIA_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "video/mp4",
    "video/webm",
)
SECRET_KEYS = {
    "ticket", "token", "authorization", "auth", "key", "signature", "sig",
    "verify", "expires", "exp", "jwt", "x-amz-signature",
    "x-amz-credential", "x-amz-security-token", "x-amz-date", "x-amz-expires",
    "pkey", "policy", "hdnts", "auth_key", "hash", "key-pair-id",
}


def is_media_url(url: str, content_type: str = "") -> bool:
    path = urlsplit(url).path.lower()
    mime = content_type.lower().split(";", 1)[0].strip()
    return path.endswith(MEDIA_EXTENSIONS) or mime in MEDIA_TYPES


def redact_url(url: str) -> str:
    """Keep the host/path while masking common signed query parameters."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    from urllib.parse import parse_qsl, urlencode, urlunsplit

    safe_query = [
        (key, "[REDACTED]" if key.lower() in SECRET_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Use a real browser session to detect media URLs requested by a video player."
    )
    parser.add_argument("--url", required=True, help="Video post URL")
    parser.add_argument("--output", default="artifacts/stream-detection")
    parser.add_argument("--timeout", type=int, default=45, help="Navigation/action timeout in seconds")
    parser.add_argument("--capture-seconds", type=int, default=20, help="Wait after attempting playback")
    parser.add_argument(
        "--resolved-output",
        help="Write the selected full media URL to a private machine-readable file for the downloader.",
    )
    parser.add_argument("--quality", type=int, help="Prefer this detected media quality, e.g. 720")
    parser.add_argument("--headed", action="store_true", help="Run headed (not recommended in CI)")
    args = parser.parse_args()

    parsed = urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--url must be an absolute HTTP or HTTPS URL")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    requests_seen: dict[str, dict] = {}
    playback_sources: list[dict] = []
    network_events: list[dict] = []
    all_links: dict[str, dict] = {}
    failed_requests: list[dict] = []
    api_responses: list[dict] = []
    page_errors: list[dict] = []
    console_errors: list[str] = []
    click_attempts: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not args.headed,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=False,
        )
        # Prevent ad/pop-under navigations from replacing the Javtiful page.
        # Media requests from the actual page may use external CDN hosts; only
        # external *top-level document navigations* are blocked here.
        page = context.new_page()
        page.set_default_timeout(min(args.timeout, 10) * 1000)
        page.set_default_navigation_timeout(args.timeout * 1000)

        def block_external_top_navigation(route) -> None:
            request = route.request
            try:
                is_main_document = (
                    request.is_navigation_request()
                    and request.frame == page.main_frame
                )
            except Exception:
                is_main_document = False
            target_host = (urlsplit(request.url).hostname or "").lower()
            if is_main_document and target_host not in {"javtiful.com", "www.javtiful.com"}:
                print(
                    f"[guard] Blocking external top-level navigation: {target_host or 'unknown host'}",
                    flush=True,
                )
                route.abort()
                return
            route.continue_()

        context.route("**/*", block_external_top_navigation)

        def close_popup(popup) -> None:
            try:
                print(f"[guard] Closing popup: {urlsplit(popup.url).hostname or 'unknown host'}", flush=True)
                popup.close()
            except Exception:
                pass

        page.on("popup", close_popup)

        def on_request(request) -> None:
            url = request.url
            kind = request.resource_type
            path_hint = url.lower()
            # Record every HTTP(S) request regardless of domain, extension, or resource type.
            if url.startswith(("http://", "https://")):
                all_links[url] = {"url": redact_url(url), "method": request.method, "resource_type": kind, "status": None}
            if kind in {"xhr", "fetch", "media"} or any(word in path_hint for word in ("playback", "playlist", "stream", "source", "video", "master")):
                network_events.append({"url": redact_url(url), "method": request.method, "resource_type": kind})
            if is_media_url(url) and not urlsplit(url).path.lower().endswith("/ping.m3u8"):
                requests_seen[url] = {
                    "url": redact_url(url),
                    "redacted_url": redact_url(url),
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "source": "request",
                }

        def on_response(response) -> None:
            url = response.url
            content_type = response.headers.get("content-type", "")
            if url.startswith(("http://", "https://")):
                all_links[url] = {"url": redact_url(url), "method": response.request.method, "resource_type": response.request.resource_type, "status": response.status, "content_type": content_type}
            if response.request.resource_type in {"xhr", "fetch", "media"} or any(word in url.lower() for word in ("playback", "playlist", "stream", "source", "video", "master")):
                network_events.append({"url": redact_url(url), "status": response.status, "resource_type": response.request.resource_type, "content_type": content_type})
            if is_media_url(url, content_type) and not urlsplit(url).path.lower().endswith("/ping.m3u8"):
                item = requests_seen.setdefault(
                    url,
                    {
                        "url": redact_url(url),
                        "redacted_url": redact_url(url),
                        "method": response.request.method,
                        "resource_type": response.request.resource_type,
                        "source": "response",
                    },
                )
                item.update(
                    {
                        "status": response.status,
                        "content_type": content_type,
                    }
                )

            response_parts = urlsplit(url)
            is_playback_api = (
                (response_parts.hostname or "").lower() in {"javtiful.com", "www.javtiful.com"}
                and "/playback" in response_parts.path.lower()
                and "json" in content_type.lower()
            )
            if is_playback_api:
                item = {"url": redact_url(url), "status": response.status, "content_type": content_type}
                try:
                    payload = response.json()
                    item["json_keys"] = sorted(payload.keys()) if isinstance(payload, dict) else []
                    sources = payload.get("sources", []) if isinstance(payload, dict) else []
                    public_sources = []
                    for source in sources if isinstance(sources, list) else []:
                        if not isinstance(source, dict) or not isinstance(source.get("src"), str):
                            continue
                        source_url = urljoin(url, source["src"].strip())
                        if urlsplit(source_url).scheme not in {"http", "https"}:
                            continue
                        try:
                            quality = int(source.get("size")) if source.get("size") is not None else None
                        except (TypeError, ValueError):
                            quality = None
                        playback_sources.append({"url": source_url, "content_type": str(source.get("type") or ""), "quality": quality})
                        public_sources.append({"src": redact_url(source_url), "type": source.get("type"), "size": quality, "label": source.get("label")})
                    item["sources"] = public_sources
                except Exception as exc:
                    item["body_parse_error"] = type(exc).__name__
                api_responses.append(item)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", lambda request: failed_requests.append({"url": redact_url(request.url), "resource_type": request.resource_type, "failure": (request.failure or "")[:300]}))
        page.on(
            "pageerror",
            lambda error: page_errors.append({
                "message": str(error)[:500],
                "stack": (getattr(error, "stack", "") or "")[:1600],
            }),
        )
        page.on(
            "console",
            lambda message: console_errors.append(message.text[:500])
            if message.type == "error" and len(console_errors) < 30
            else None,
        )

        navigation_error = None
        print(f"[1/5] Navigating to {args.url}", flush=True)
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            print("[2/5] Page DOM loaded; allowing player scripts to initialize", flush=True)
            page.wait_for_timeout(2500)
        except PlaywrightTimeoutError as exc:
            navigation_error = f"Navigation timeout: {type(exc).__name__}"
            print(f"[!] {navigation_error}; continuing with the current page", flush=True)

        print("[3/5] Looking for a visible Play control", flush=True)
        # Try common player controls first. Clicking a real control best matches
        # the user's manual Play action and triggers the normal player lifecycle.
        selectors = [
            # Match the working Colab notebook's actual Javtiful play gate first.
            ".front-playback-start",
            ".front-playback-start-center",
            "button[aria-label='Play video']",
            "button[aria-label*='Play' i]",
            "button[aria-label*='play' i]",
            ".plyr__control--overlaid",
            "button[title*='play' i]",
            "#front-player",
            "video",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    # The working notebook force-clicks the gate; the overlay can
                    # intercept a normal click even when the button is visible.
                    locator.click(timeout=3000, force=True)
                    click_attempts.append(selector)
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        # Reproduce the notebook's second gate click, then request muted playback.
        # Do not await video.play(): its promise may remain pending indefinitely.
        print("[4/5] Re-triggering the Javtiful play gate and requesting muted playback", flush=True)
        try:
            gate_state = page.evaluate(
                """() => {
                    const button = document.querySelector(
                        '.front-playback-start, button[aria-label*="Play" i]'
                    );
                    if (button) button.click();

                    // Javtiful uses <video id="front-player">, not a video nested
                    // inside an element with that ID.
                    const video = document.querySelector('#front-player, video');
                    if (video) {
                        video.muted = true;
                        const pending = video.play();
                        if (pending && typeof pending.catch === 'function') {
                            pending.catch(() => {});
                        }
                    }
                    return {
                        gate_found: !!button,
                        video_found: !!video,
                        video_muted: video ? video.muted : null
                    };
                }"""
            )
            if isinstance(gate_state, dict):
                click_attempts.append("javascript:.front-playback-start")
        except Exception as exc:
            page_errors.append({
                "message": f"Player activation evaluation failed: {type(exc).__name__}: {exc}"[:500],
                "stack": "",
            })

        print(f"[5/5] Capturing media network requests for {args.capture_seconds}s", flush=True)
        page.wait_for_timeout(max(1, args.capture_seconds) * 1000)
        print(f"[done] Capture window complete; {len(requests_seen)} media request(s) detected", flush=True)

        page_state = {}
        try:
            page_state = page.evaluate(
                r"""() => {
                    const videos = [...document.querySelectorAll('video')];
                    const resources = performance.getEntriesByType('resource').map(e => e.name);
                    return {
                        video_count: videos.length,
                        videos: videos.map(v => ({
                            current_src: v.currentSrc || null,
                            src_attribute: v.getAttribute('src'),
                            ready_state: v.readyState,
                            network_state: v.networkState,
                            paused: v.paused,
                            duration: Number.isFinite(v.duration) ? v.duration : null,
                            error_code: v.error ? v.error.code : null,
                            error_message: v.error ? v.error.message : null,
                            buffered_ranges: v.buffered ? v.buffered.length : 0
                        })),
                        iframes: [...document.querySelectorAll('iframe')].map(f => f.src || f.getAttribute('src')).filter(Boolean),
                        player_scripts: [...document.scripts].map(s => s.src).filter(Boolean),
                        player_controls: [...document.querySelectorAll('button[aria-label*="play" i], button[title*="play" i], .plyr__control--overlaid')].slice(0, 8).map(b => ({
                            aria_label: b.getAttribute('aria-label'),
                            title: b.getAttribute('title'),
                            class_name: String(b.className || '').slice(0, 160),
                            text: (b.innerText || '').slice(0, 100),
                            visible: !!(b.offsetWidth || b.offsetHeight || b.getClientRects().length),
                            disabled: !!b.disabled,
                            parent_class: String(b.parentElement?.className || '').slice(0, 160)
                        })),
                        player_container: (() => {
                            const el = document.querySelector('#front-player, .plyr, .video-player, .watch-player');
                            return el ? el.outerHTML.slice(0, 3000) : null;
                        })(),
                        media_resource_entries: resources.filter(u => /playback|playlist|stream|source|video|master|\.m3u8|\.mpd|\.mp4|\.m4v|\.webm|\.m4s|\.ts/i.test(u)).slice(-100)
                    };
                }"""
            )
        except Exception:
            pass

        final_url = page.url
        final_host = (urlsplit(final_url).hostname or "").lower()
        if final_host not in {"javtiful.com", "www.javtiful.com"}:
            # Fail closed: never accept media captured after leaving the target site.
            requests_seen.clear()
            navigation_error = (
                f"Target page left Javtiful unexpectedly (final host: {final_host or 'unknown'}). "
                "External media requests were discarded."
            )
        page_state["iframes"] = [
            {"host": urlsplit(u).hostname, "path": urlsplit(u).path[:180]}
            for u in page_state.get("iframes", [])
        ]
        page_state["player_scripts"] = [{"url": redact_url(u)} for u in page_state.get("player_scripts", [])]
        page_state["media_resource_entries"] = [redact_url(u) for u in page_state.get("media_resource_entries", [])]
        for video in page_state.get("videos", []):
            if isinstance(video, dict):
                for key in ("current_src", "src_attribute"):
                    if isinstance(video.get(key), str):
                        video[key] = redact_url(video[key])
        context.close()
        browser.close()

    media = list(requests_seen.values())
    # Prioritize playlists, then direct media files.
    media.sort(
        key=lambda item: (
            0 if urlsplit(item["url"]).path.lower().endswith(".m3u8") else
            1 if urlsplit(item["url"]).path.lower().endswith(".mpd") else
            2 if urlsplit(item["url"]).path.lower().endswith((".mp4", ".m4v")) else 3
        )
    )
    selected_playback_source = None
    if final_host in {"javtiful.com", "www.javtiful.com"} and playback_sources:
        candidates = playback_sources
        if args.quality is not None:
            exact = [item for item in candidates if item.get("quality") == args.quality]
            if exact:
                candidates = exact
        selected_playback_source = max(candidates, key=lambda item: item.get("quality") or 0)

    report = {
        "requested_url": args.url,
        "final_page_url": final_url if "final_url" in locals() else None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "navigation_error": navigation_error,
        "play_click_attempts": click_attempts,
        "capture_seconds_after_play": args.capture_seconds,
        "media_request_count": len(media),
        "media_requests": media,
        "all_links_count": len(all_links),
        "all_links": list(all_links.values())[-2000:],
        "network_events": network_events[-500:],
        "failed_requests": failed_requests[-200:],
        "playback_api_responses": api_responses,
        "video_elements": page_state,
        "page_errors": page_errors[:20],
        "console_errors": console_errors[:30],
        "notes": [
            "All HTTP(S) network links are recorded regardless of domain, extension, or resource type; media classification remains separate.",
            "Media URLs in this report are redacted; full signed URLs are written only to the optional local --resolved-output file.",
            "Never upload or commit the --resolved-output file because signed media URLs are short-lived credentials.",
            "Playback tickets and common signed query parameters are redacted from report URLs.",
            "External top-level navigations and popups are blocked to prevent ad/pop-under pages from being mistaken for the target stream.",
            "Media captured after the main page leaves javtiful.com is discarded.",
            "An empty media list means playback was not triggered or the stream was delivered through an unclassified mechanism.",
        ],
    }

    report_path = output / "stream-detection.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # This optional file is intended only for the local downloader process.
    # It contains a short-lived signed URL, so never print it or upload it as an artifact.
    if args.resolved_output and (selected_playback_source or media):
        def quality_from_item(item: dict) -> int | None:
            candidates = [
                str(item.get("url", "")),
                str(item.get("content_type", "")),
            ]
            for candidate in candidates:
                match = re.search(r"(?:_|/)(\d{3,4})p(?:\.|/|$)", candidate, re.IGNORECASE)
                if match:
                    return int(match.group(1))
            return None

        matching_quality = [
            item for item in media
            if args.quality is not None and quality_from_item(item) == args.quality
        ]
        selected = matching_quality[0] if matching_quality else (media[0] if media else None)
        resolved_payload = {
            "url": selected_playback_source["url"] if selected_playback_source else next(
                (raw_url for raw_url in requests_seen if selected and redact_url(raw_url) == selected["redacted_url"]),
                None,
            ),
            "content_type": selected_playback_source.get("content_type", "") if selected_playback_source else (selected or {}).get("content_type", ""),
            "status": 200 if selected_playback_source else (selected or {}).get("status"),
            "quality": selected_playback_source.get("quality") if selected_playback_source else quality_from_item(selected or {}),
            "duration_seconds": next(
                (
                    video.get("duration")
                    for video in page_state.get("videos", [])
                    if isinstance(video.get("duration"), (int, float))
                ),
                None,
            ),
            "post_url": final_url,
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        }
        if not resolved_payload["url"]:
            raise RuntimeError("Could not map the selected media request to its original URL.")
        resolved_path = Path(args.resolved_output)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(
            json.dumps(resolved_payload, ensure_ascii=False),
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "final_page_url": report["final_page_url"],
                "navigation_error": navigation_error,
                "play_click_attempts": click_attempts,
                "media_request_count": len(media),
                "all_links_count": len(all_links),
                "network_event_count": len(network_events),
                "failed_request_count": len(failed_requests),
                "video_count": page_state.get("video_count", 0),
                "iframe_count": len(page_state.get("iframes", [])),
                "media_requests": [
                    {
                        "redacted_url": item["redacted_url"],
                        "status": item.get("status"),
                        "content_type": item.get("content_type"),
                    }
                    for item in media
                ],
                "playback_api_responses": api_responses[-10:],
                "video_state": page_state.get("videos", []),
                "iframe_hosts": page_state.get("iframes", []),
                "playback_network_events": [
                    event for event in network_events
                    if "playback" in str(event.get("url", "")).lower()
                    or "media/video" in str(event.get("url", "")).lower()
                ][-12:],
                "player_controls": page_state.get("player_controls", []),
                "player_container": page_state.get("player_container"),
                "media_resource_entries": page_state.get("media_resource_entries", [])[-12:],
                "failed_requests": failed_requests[-12:],
                "page_errors": page_errors[-10:],
                "console_errors": console_errors[-10:],
                "report": str(report_path),
            },
            indent=2,
        )
    )
    return 0 if media else 2


if __name__ == "__main__":
    raise SystemExit(main())
