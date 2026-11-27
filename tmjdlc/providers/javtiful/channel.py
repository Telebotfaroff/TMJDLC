"""Javtiful channel listing and pagination helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

CHANNEL_RE = re.compile(r"^/channel/([A-Za-z0-9_-]+)/?$", re.I)
VIDEO_RE = re.compile(r"^/video/(\d+)(?:[-/]|$)", re.I)
TOTAL_RE = re.compile(r"([\d,]+)\s+Videos?", re.I)
PAGE_SIZE = 23
BASE_URL = "https://javtiful.com"


@dataclass(frozen=True)
class ChannelInfo:
    url: str
    slug: str
    name: str
    total_videos: int
    total_pages: int
    last_page_count: int


def normalize_channel_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"javtiful.com", "www.javtiful.com"}:
        raise ValueError("Send a Javtiful channel URL, for example https://javtiful.com/channel/madonna")
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
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))


def parse_listing(html: str, base_url: str = BASE_URL) -> tuple[str, int | None, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one("h1, h2.channel-title, .channel-title, .profile-name")
    name = heading.get_text(" ", strip=True) if heading else ""
    if not name:
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        name = re.sub(r"\s*[-|]\s*Javtiful.*$", "", title, flags=re.I).strip()

    text = soup.get_text(" ", strip=True)
    total_match = TOTAL_RE.search(text)
    total = int(total_match.group(1).replace(",", "")) if total_match else None

    urls: list[str] = []
    seen_video_ids: set[str] = set()

    # The site has used both ordinary anchors and data-* attributes for video
    # cards. Collect all known URL-bearing attributes, then normalize them
    # through urljoin so relative and absolute links behave identically.
    candidates: list[str] = []
    for element in soup.select("a[href], [data-href], [data-url], [data-video-url], [data-link]"):
        for attribute in ("href", "data-href", "data-url", "data-video-url", "data-link"):
            value = element.get(attribute)
            if value:
                candidates.append(str(value).strip())

    # Some listing templates place video paths in inline scripts/JSON instead
    # of an anchor. Scan only the recognizable Javtiful video path pattern.
    candidates.extend(re.findall(r"""(?:https?://(?:www\.)?javtiful\.com)?/video/\d+(?:[-/][^\s"'<>\\]*)?""", html, re.I))

    for candidate in candidates:
        absolute = urljoin(base_url, candidate)
        parsed = urlsplit(absolute)
        if parsed.hostname not in {"javtiful.com", "www.javtiful.com"}:
            continue
        path = parsed.path
        match = VIDEO_RE.match(path)
        if not match:
            continue
        # Javtiful can expose multiple slugs/links for the same video ID.
        # Treat the numeric ID as the identity so duplicate cards collapse.
        video_id = match.group(1)
        if video_id in seen_video_ids:
            continue
        seen_video_ids.add(video_id)
        urls.append(f"{BASE_URL}{path}")
    return name, total, urls


def parse_channel_links(html: str, base_url: str = BASE_URL) -> list[dict[str, str]]:
    """Extract unique Javtiful channel-page URLs and their visible anchor labels."""
    soup = BeautifulSoup(html, "lxml")
    channels: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        absolute = urljoin(base_url, anchor.get("href", "").strip())
        parsed = urlsplit(absolute)
        match = CHANNEL_RE.fullmatch(parsed.path.rstrip("/"))
        if parsed.hostname not in {"javtiful.com", "www.javtiful.com"} or not match:
            continue
        url = f"{BASE_URL}/channel/{match.group(1)}"
        slug = match.group(1)
        name = anchor.get_text(" ", strip=True) or re.sub(r"[-_]+", " ", slug).title()
        channels[url.lower()] = {"url": url, "slug": slug, "name": name}
    return list(channels.values())


class JavtifulChannelScraper:
    def __init__(self, *, timeout: int = 25, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def fetch_page(self, channel_url: str, page: int) -> tuple[str, int | None, list[str]]:
        response = self.session.get(page_url(channel_url, page), timeout=self.timeout)
        response.raise_for_status()
        return parse_listing(response.text)

    def discover_channels(
        self,
        seed_urls: list[str] | None = None,
        *,
        max_pages: int = 100,
    ) -> list[dict[str, str]]:
        """Crawl Javtiful's public listing/navigation pages and collect channel URLs.

        The crawl is deliberately bounded and only follows same-site listing,
        category, creator, pagination, and homepage links. Video pages are skipped.
        """
        seeds = seed_urls or [
            BASE_URL,
            f"{BASE_URL}/channels",
            f"{BASE_URL}/channels/",
            f"{BASE_URL}/channel",
            f"{BASE_URL}/channel/",
            f"{BASE_URL}/models",
            f"{BASE_URL}/models/",
            f"{BASE_URL}/creators",
            f"{BASE_URL}/pornstars",
            f"{BASE_URL}/categories",
        ]
        pending = list(dict.fromkeys(seeds))
        visited: set[str] = set()
        found: dict[str, dict[str, str]] = {}
        max_pages = max(1, min(500, int(max_pages)))

        while pending and len(visited) < max_pages:
            current = pending.pop(0)
            parsed_current = urlsplit(current)
            if parsed_current.hostname not in {"javtiful.com", "www.javtiful.com"}:
                continue
            normalized = urlunsplit((
                parsed_current.scheme or "https", parsed_current.netloc,
                parsed_current.path or "/", parsed_current.query, "",
            ))
            if normalized in visited:
                continue
            visited.add(normalized)
            try:
                response = self.session.get(normalized, timeout=self.timeout)
                response.raise_for_status()
            except requests.RequestException:
                continue

            for item in parse_channel_links(response.text, normalized):
                found[item["url"].lower()] = item

            soup = BeautifulSoup(response.text, "lxml")
            for anchor in soup.select("a[href]"):
                absolute = urljoin(normalized, anchor.get("href", "").strip())
                parsed = urlsplit(absolute)
                if parsed.hostname not in {"javtiful.com", "www.javtiful.com"}:
                    continue
                if CHANNEL_RE.fullmatch(parsed.path.rstrip("/")) or VIDEO_RE.match(parsed.path.rstrip("/")):
                    continue
                path = parsed.path.lower().rstrip("/") or "/"
                query = parse_qs(parsed.query)
                listing_path = (
                    path in {"/", "/channels", "/models", "/creators", "/categories"}
                    or any(token in path for token in ("/channels", "/models", "/creators", "/categories", "/pornstars"))
                    or "page" in query
                )
                if listing_path and absolute not in visited and absolute not in pending:
                    pending.append(absolute)

        return sorted(found.values(), key=lambda item: item["name"].casefold())

    def inspect(self, url: str) -> ChannelInfo:
        channel_url = normalize_channel_url(url)
        first_name, total, first_urls = self.fetch_page(channel_url, 1)
        if total is None:
            raise RuntimeError("Couldn't read the channel's video count from its page.")
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
            name=first_name or urlsplit(channel_url).path.rsplit("/", 1)[-1],
            total_videos=actual_total,
            total_pages=pages,
            last_page_count=last_count,
        )

    def videos_for_pages(self, info: ChannelInfo, pages: list[int]) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for page in sorted(set(pages)):
            if page < 1 or page > info.total_pages:
                raise ValueError(f"Page {page} is outside 1-{info.total_pages}.")
            _, _, page_urls = self.fetch_page(info.url, page)
            for url in page_urls:
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
        return urls
