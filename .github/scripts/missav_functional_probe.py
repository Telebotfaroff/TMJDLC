#!/usr/bin/env python3
"""Lightweight MissAV functional smoke test.

Checks search, video metadata, HLS manifest retrieval, and a bounded byte-range
request against the first media segment. Never logs signed URLs, cookies, or
response bodies.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from tmjdlc.providers.missav import MissAVProvider

DEFAULT_URL = "https://missav.ws/en/rlmp-013-uncensored-leak"
DEFAULT_QUERY = "uncensored"
REPORT_PATH = Path(os.getenv("MISSAV_REPORT_PATH", "artifacts/missav_functional_probe.json"))
RANGE_BYTES = 262_144


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"missav.ws", "www.missav.ws"}:
        raise ValueError("MISSAV_TEST_URL must be an HTTPS URL on missav.ws.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("MISSAV_TEST_URL must point to a video page.")
    return url


def first_playlist_uri(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Prefer a variant playlist when the endpoint returns a master playlist.
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for candidate in lines[index + 1:]:
                if not candidate.startswith("#"):
                    return candidate
    # Otherwise this is likely a media playlist; return its first segment.
    for line in lines:
        if not line.startswith("#"):
            return line
    return None


async def run_probe(url: str, query: str) -> dict[str, object]:
    parsed = urlparse(url)
    report: dict[str, object] = {
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": "missav",
        "target_host": parsed.hostname,
        "target_path": parsed.path,
        "search": {"status": "NOT_RUN", "results_seen": 0, "title_present": False},
        "metadata": {"status": "NOT_RUN", "title_present": False},
        "hls_manifest": {"status": "NOT_RUN"},
        "hls_segment_sample": {
            "status": "NOT_RUN",
            "range_requested_bytes": RANGE_BYTES,
            "bytes_received": 0,
        },
        "elapsed_seconds": None,
        "errors": {},
    }
    started = time.monotonic()

    async with MissAVProvider() as provider:
        # Search smoke test: consume up to five results and validate the first item.
        try:
            async for item in provider.search(query, video_count=5):
                report["search"]["results_seen"] += 1
                try:
                    found_video = item.unwrap()
                    report["search"]["title_present"] = bool(found_video.title)
                except Exception as exc:
                    report["errors"]["search_item"] = type(exc).__name__
                if report["search"]["results_seen"] >= 5:
                    break
            report["search"]["status"] = (
                "SUCCESS" if report["search"]["results_seen"] > 0
                and report["search"]["title_present"] else "NO_RESULTS_OR_EMPTY"
            )
        except Exception as exc:
            report["search"]["status"] = "FAILED"
            report["errors"]["search"] = type(exc).__name__

        # Fetch the known page and use the provider's parsed HLS playlist URL.
        video = None
        try:
            video = await provider.get_video(url, load_html=True)
            report["metadata"]["title_present"] = bool(video.title)
            report["metadata"]["status"] = (
                "SUCCESS" if video.title else "EMPTY_METADATA"
            )
        except Exception as exc:
            report["metadata"]["status"] = "FAILED"
            report["errors"]["metadata"] = type(exc).__name__

        if video is not None and video.m3u8_base_url:
            session = provider.client.core.session
            try:
                manifest_response = await session.get(video.m3u8_base_url, timeout=15)
                manifest_response.raise_for_status()
                manifest_text = manifest_response.text
                report["hls_manifest"]["status"] = (
                    "SUCCESS" if "#EXTM3U" in manifest_text else "INVALID_MANIFEST"
                )

                segment_ref = first_playlist_uri(manifest_text)
                if segment_ref:
                    segment_url = urljoin(str(manifest_response.url), segment_ref)
                    # Bound the transfer to a small prefix of one segment.
                    segment_response = await session.get(
                        segment_url,
                        headers={"Range": f"bytes=0-{RANGE_BYTES - 1}"},
                        timeout=20,
                    )
                    segment_response.raise_for_status()
                    payload = segment_response.content
                    report["hls_segment_sample"]["bytes_received"] = min(
                        len(payload), RANGE_BYTES
                    )
                    report["hls_segment_sample"]["http_status"] = segment_response.status_code
                    report["hls_segment_sample"]["status"] = (
                        "SUCCESS" if payload else "EMPTY_RESPONSE"
                    )
                else:
                    report["hls_segment_sample"]["status"] = "NO_SEGMENT_REFERENCE"
            except Exception as exc:
                report["hls_manifest"]["status"] = (
                    "FAILED" if report["hls_manifest"]["status"] == "NOT_RUN"
                    else report["hls_manifest"]["status"]
                )
                report["hls_segment_sample"]["status"] = "FAILED"
                report["errors"]["hls"] = type(exc).__name__
        elif video is not None:
            report["hls_manifest"]["status"] = "NO_PLAYLIST_URL"
            report["hls_segment_sample"]["status"] = "SKIPPED"

    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main() -> int:
    try:
        url = validate_url(os.getenv("MISSAV_TEST_URL", DEFAULT_URL).strip())
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    query = os.getenv("MISSAV_SEARCH_QUERY", DEFAULT_QUERY).strip() or DEFAULT_QUERY
    report = asyncio.run(run_probe(url, query))
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))

    checks = (
        report["search"]["status"] == "SUCCESS"
        and report["metadata"]["status"] == "SUCCESS"
        and report["hls_manifest"]["status"] == "SUCCESS"
        and report["hls_segment_sample"]["status"] == "SUCCESS"
    )
    return 0 if checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
