from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
PLAYER_HINTS = ("watch-player", "watch.js", "player", "hls", "plyr")
SECRET_PATTERNS = [
    (re.compile(r'(?i)(["\']?ticket["\']?\s*:\s*["\'])[^"\']+(["\'])'), r'\1[REDACTED]\2'),
    (re.compile(r'(?i)(["\']?token["\']?\s*[:=]\s*["\'])[^"\']+(["\'])'), r'\1[REDACTED]\2'),
    (re.compile(r'(?i)(authorization\s*[:=]\s*["\'])[^"\']+(["\'])'), r'\1[REDACTED]\2'),
]


def redact_text(value: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a post page and its player JavaScript using HTTP requests."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", default="artifacts/player-inspection")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    parsed = urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--url must be an absolute HTTP or HTTPS URL")

    output = Path(args.output)
    scripts_dir = output / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    response = session.get(args.url, timeout=args.timeout)
    response.raise_for_status()
    html = response.text
    soup = BeautifulSoup(html, "lxml")

    # Save a sanitized HTML copy for review; do not publish transient playback tickets.
    sanitized_html = redact_text(html)
    (output / "post-page-sanitized.html").write_text(sanitized_html, encoding="utf-8")

    inline_config = []
    for index, script in enumerate(soup.find_all("script")):
        if script.get("src"):
            continue
        text = script.string or script.get_text()
        if text and any(word in text.lower() for word in ("playback", "ticket", "playlist", "front-player")):
            name = f"inline-player-config-{index:03d}.js"
            (scripts_dir / name).write_text(redact_text(text), encoding="utf-8")
            endpoints = sorted(set(re.findall(
                r'["\']([^"\']*(?:playback|playlist)[^"\']*)["\']', text, re.I
            )))
            inline_config.append(
                {
                    "file": name,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "endpoint_candidates": endpoints,
                    "contains_playback_ticket": bool(re.search(r'(?i)["\']ticket["\']\s*:', text)),
                }
            )

    script_entries = []
    seen = set()
    for tag in soup.find_all("script", src=True):
        src = urljoin(response.url, tag["src"])
        path = urlsplit(src).path.lower()
        if not any(hint in path for hint in PLAYER_HINTS):
            continue
        if src in seen:
            continue
        seen.add(src)
        entry = {"url": safe_url(src), "status": None, "file": None, "error": None}
        try:
            script_response = session.get(
                src,
                timeout=args.timeout,
                headers={"Referer": response.url},
            )
            entry["status"] = script_response.status_code
            entry["content_type"] = script_response.headers.get("content-type")
            if script_response.ok:
                filename = f"asset-{len(script_entries) + 1:02d}-{Path(path).name or 'player.js'}"
                body = script_response.text
                (scripts_dir / filename).write_text(redact_text(body), encoding="utf-8")
                entry.update(
                    {
                        "file": filename,
                        "bytes": len(script_response.content),
                        "sha256": hashlib.sha256(script_response.content).hexdigest(),
                    }
                )
            else:
                entry["error"] = f"HTTP {script_response.status_code}"
        except requests.RequestException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        script_entries.append(entry)

    player = soup.select_one("#front-player")
    playback_paths = sorted(
        set(re.findall(r'["\']([^"\']*/(?:playback|playlist)[^"\']*)["\']', html, re.I))
    )
    report = {
        "requested_url": safe_url(args.url),
        "final_url": safe_url(response.url),
        "http_status": response.status_code,
        "page_title": soup.title.get_text(" ", strip=True) if soup.title else None,
        "canonical_url": (
            soup.select_one('link[rel="canonical"]').get("href")
            if soup.select_one('link[rel="canonical"]')
            else None
        ),
        "player_element": (
            {
                "id": player.get("id"),
                "tag": player.name,
                "attributes": {
                    key: value
                    for key, value in player.attrs.items()
                    if key not in {"src"}
                },
            }
            if player
            else None
        ),
        "playback_endpoint_candidates": playback_paths,
        "inline_player_config": inline_config,
        "player_assets": script_entries,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "notes": [
            "This inspection fetches page HTML and referenced player JavaScript only.",
            "It does not call playback endpoints or publish temporary playback tickets.",
            "Review the JavaScript to determine the exact playback request contract before testing it.",
        ],
    }
    write_json(output / "player-inspection.json", report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
