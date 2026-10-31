#!/usr/bin/env python3
"""One-request MissAV API smoke test for CI.

Only reports metadata-fetch success/failure. It never downloads media and never
writes signed media URLs, cookies, response bodies, or exception messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from tmjdlc.providers.missav import MissAVProvider

DEFAULT_URL = "https://missav.ws/en/rlmp-013-uncensored-leak"
REPORT_PATH = Path(os.getenv("MISSAV_REPORT_PATH", "artifacts/missav_probe.json"))


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"missav.ws", "www.missav.ws"}:
        raise ValueError("MISSAV_TEST_URL must be an HTTPS URL on missav.ws.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("MISSAV_TEST_URL must point to a video page, not the homepage.")
    return url


def classify_error(exc: BaseException) -> str:
    names: set[str] = set()
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        names.add(type(current).__name__.lower())
        # Inspect internally for classification only. Never persist or print this
        # string because upstream exceptions may contain request URLs.
        messages.append(str(current).lower())
        current = current.__cause__ or current.__context__

    joined = " ".join(messages)
    if "accessdeniederror" in names:
        return "ACCESS_DENIED"
    if "403" in joined or "request blocked" in joined or "cloudflare" in joined:
        return "BLOCKED_OR_CHALLENGED"
    if "medialoaderror" in names:
        return "MEDIA_LOAD_ERROR"
    if "timeout" in joined or any("timeout" in name for name in names):
        return "TIMEOUT"
    return "API_ERROR"


async def run_probe(url: str) -> dict[str, object]:
    parsed = urlparse(url)
    report: dict[str, object] = {
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": "missav",
        "target_host": parsed.hostname,
        "target_path": parsed.path,
        "result": "UNKNOWN",
        "metadata_loaded": False,
        "title_present": False,
        "elapsed_seconds": None,
        "exception_type": None,
    }

    started = time.monotonic()
    try:
        async with MissAVProvider() as provider:
            video = await provider.get_video(url, load_html=True)
            metadata = provider.metadata(video)

        title = metadata.get("title")
        if not title:
            report["result"] = "EMPTY_METADATA"
            report["metadata_loaded"] = True
        else:
            report["result"] = "SUCCESS"
            report["metadata_loaded"] = True
            report["title_present"] = True
    except Exception as exc:  # CI report must capture upstream/network failures.
        report["result"] = classify_error(exc)
        report["exception_type"] = type(exc).__name__
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)

    return report


def main() -> int:
    try:
        url = validate_url(os.getenv("MISSAV_TEST_URL", DEFAULT_URL).strip())
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    report = asyncio.run(run_probe(url))
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
