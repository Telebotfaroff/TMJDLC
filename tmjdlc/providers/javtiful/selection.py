"""Page and video selection helpers for the Javtiful scanner.

Selections are one-based so the same syntax can be used in Telegram messages
and notebook prompts: "all", "1,3,5-8".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from tmjdlc.providers.javtiful.provider import JavtifulURLParser

_SELECTION_TOKEN = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?$")
_VIDEO_ID = re.compile(r"^/video/(\d+)(?:/|$)", re.I)


@dataclass(frozen=True, slots=True)
class VideoCandidate:
    index: int
    url: str
    code: str | None


@dataclass(frozen=True, slots=True)
class VideoSelection:
    selected: tuple[VideoCandidate, ...]
    skipped_duplicates: tuple[VideoCandidate, ...]
    discovered_count: int


def parse_number_selection(value: str, *, maximum: int) -> list[int]:
    """Parse 'all', comma-separated numbers and inclusive ranges."""
    if maximum < 1:
        raise ValueError("There are no items available to select.")
    raw = (value or "").strip().lower()
    if raw in {"all", "*"}:
        return list(range(1, maximum + 1))
    if not raw:
        raise ValueError("Enter 'all' or numbers, e.g. 1,3,5-8.")

    selected: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        match = _SELECTION_TOKEN.fullmatch(token)
        if not match:
            raise ValueError(f"Invalid selection item: {token!r}. Use numbers and ranges.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > maximum:
            raise ValueError(f"Selection {token!r} is outside the valid range 1-{maximum}.")
        for number in range(start, end + 1):
            if number not in seen:
                seen.add(number)
                selected.append(number)
    return selected


def _video_identity(url: str) -> str:
    parsed = urlsplit(url)
    match = _VIDEO_ID.match(parsed.path)
    return match.group(1) if match else url


def select_videos(
    video_urls: list[str] | tuple[str, ...],
    selection: str,
    *,
    completed_codes: set[str] | frozenset[str] = frozenset(),
    parser: JavtifulURLParser | None = None,
) -> VideoSelection:
    """Select discovered videos by one-based position and remove duplicates."""
    if not video_urls:
        raise ValueError("No videos were discovered. Scan a channel page first.")

    indexes = parse_number_selection(selection, maximum=len(video_urls))
    url_parser = parser or JavtifulURLParser()
    completed = {code.strip().upper() for code in completed_codes if code.strip()}

    candidates = []
    for index, url in enumerate(video_urls, start=1):
        source = url_parser.parse(url)
        candidates.append(VideoCandidate(index=index, url=source.url, code=source.code))

    chosen = set(indexes)
    selected: list[VideoCandidate] = []
    skipped: list[VideoCandidate] = []
    seen_ids: set[str] = set()
    seen_codes: set[str] = set()

    for candidate in candidates:
        if candidate.index not in chosen:
            continue
        identity = _video_identity(candidate.url)
        code = candidate.code.upper() if candidate.code else None
        duplicate = identity in seen_ids or (code is not None and code in seen_codes)
        already_saved = code is not None and code in completed
        seen_ids.add(identity)
        if code:
            seen_codes.add(code)
        if duplicate or already_saved:
            skipped.append(candidate)
        else:
            selected.append(candidate)

    return VideoSelection(
        selected=tuple(selected),
        skipped_duplicates=tuple(skipped),
        discovered_count=len(video_urls),
    )
