"""Javtiful channel listing and pagination helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

CHANNEL_RE = re.compile(r"^/channel/([A-Za-z0-9_-]+)/?$", re.I)
VIDEO_RE = re.compile(r"^/video/(\d+)(?:/|$)", re.I)
TOTAL_RE = re.compile(r"([\d,]+)\s+Videos?", re.I)
PAGE_SIZE = 23
BASE_URL = "https://javtiful.com"


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    url: str
    slug: str
    name: str
    total_videos: int
    total_pages: int
    last_page_count: int


def normalize_channel_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or host not in {"javtiful.com", "www.javtiful.com"}
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "Send a Javtiful channel URL, for example "
            "https://javtiful.com/channel/madonna"
        )
    match = CHANNEL_RE.fullmatch(parsed.path)
    if not match:
        raise ValueError("Expected a Javtiful /channel/SLUG URL.")
    return f"{BASE_URL}/channel/{match.group(1)}"


def page_url(channel_url: str, page: int) -> str:
    if page < 1:
        raise ValueError("Page number must be at least 1.")
    parsed = urlsplit(channel_url)
    query = parse_qs(parsed.query)
    if page == 1:
        query.pop("page", None)
    else:
        query["page"] = [str(page)]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), "")
    )


def parse_listing(
    html: str, base_url: str = BASE_URL
) -> tuple[str, int | None, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one("h1, h2.channel-title, .channel-title, .profile-name")
    name = heading.get_text(" ", strip=True) if heading else ""
    if not name:
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        name = re.sub(r"\s*[-|]\s*Javtiful.*$", "", title, flags=re.I).strip()

    total_match = TOTAL_RE.search(soup.get_text(" ", strip=True))
    total = int(total_match.group(1).replace(",", "")) if total_match else None

    urls: list[str] = []
    seen_video_ids: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        parsed_href = urlsplit(href)
        if parsed_href.netloc and (parsed_href.hostname or "").lower() not in {
            "javtiful.com", "www.javtiful.com"
        }:
            continue
        match = VIDEO_RE.match(parsed_href.path)
        if not match:
            continue
        video_id = match.group(1)
        if video_id in seen_video_ids:
            continue
        seen_video_ids.add(video_id)
        urls.append(f"{base_url.rstrip('/')}{parsed_href.path}")
    return name, total, urls


class JavtifulChannelScraper:
    """Inspect channel pagination and fetch only the pages selected by the user."""

    def __init__(
        self, *, timeout: int = 25, session: requests.Session | None = None
    ):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def fetch_page(
        self, channel_url: str, page: int
    ) -> tuple[str, int | None, list[str]]:
        response = self.session.get(
            page_url(channel_url, page), timeout=self.timeout
        )
        response.raise_for_status()
        return parse_listing(response.text)

    def inspect(self, url: str) -> ChannelInfo:
        channel_url = normalize_channel_url(url)
        first_name, total, first_urls = self.fetch_page(channel_url, 1)
        if total is None:
            raise RuntimeError(
                "Couldn't read the channel's video count from its page."
            )
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if pages > 1:
            _, _, last_urls = self.fetch_page(channel_url, pages)
            last_count = len(last_urls)
        else:
            last_count = len(first_urls)
        actual_total = PAGE_SIZE * (pages - 1) + last_count
        return ChannelInfo(
            url=channel_url,
            slug=urlsplit(channel_url).path.rsplit("/", 1)[-1],
            name=first_name
            or urlsplit(channel_url).path.rsplit("/", 1)[-1],
            total_videos=actual_total,
            total_pages=pages,
            last_page_count=last_count,
        )

    def videos_for_pages(self, info: ChannelInfo, pages: list[int]) -> list[str]:
        urls: list[str] = []
        seen_ids: set[str] = set()
        for page in sorted(set(pages)):
            if page < 1 or page > info.total_pages:
                raise ValueError(
                    f"Page {page} is outside 1-{info.total_pages}."
                )
            _, _, page_urls = self.fetch_page(info.url, page)
            for url in page_urls:
                video_id = VIDEO_RE.match(urlsplit(url).path)
                identity = video_id.group(1) if video_id else url
                if identity not in seen_ids:
                    seen_ids.add(identity)
                    urls.append(url)
        return urls
