from urllib.parse import parse_qs, urlsplit

import pytest

from tmjdlc.providers.javtiful.channel import (
    JavtifulChannelScraper,
    normalize_channel_url,
    page_url,
    parse_listing,
)


def listing(count: int, *, total: str = "2,123 Videos", name: str = "Madonna") -> str:
    anchors = "".join(
        f'<a href="/video/{1000 + i}/sample">Video {i}</a>' for i in range(count)
    )
    return f"<html><head><title>{name} - Javtiful</title></head><body><h1>{name}</h1><p>{total}</p>{anchors}</body></html>"


class FakeResponse:
    def __init__(self, html: str):
        self.text = html

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.urls = []

    def get(self, url: str, timeout: int):
        self.urls.append(url)
        page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        return FakeResponse(listing(23 if page == 1 else 7))


def test_normalize_and_build_page_urls():
    assert normalize_channel_url("https://www.javtiful.com/channel/madonna/") == (
        "https://javtiful.com/channel/madonna"
    )
    assert page_url("https://javtiful.com/channel/madonna", 2).endswith("?page=2")
    with pytest.raises(ValueError):
        normalize_channel_url("https://example.com/channel/madonna")


def test_parse_listing_extracts_count_name_and_unique_posts():
    html = listing(2).replace(
        '<a href="/video/1001/sample">Video 1</a>',
        '<a href="/video/1000/duplicate">Duplicate</a>',
    )
    name, total, urls = parse_listing(html)
    assert name == "Madonna"
    assert total == 2123
    assert urls == ["https://javtiful.com/video/1000/sample"]


def test_inspect_uses_actual_last_page_count():
    session = FakeSession()
    info = JavtifulChannelScraper(session=session).inspect(
        "https://javtiful.com/channel/madonna"
    )
    assert info.name == "Madonna"
    assert info.total_pages == 93
    assert info.last_page_count == 7
    assert info.total_videos == 2123
    assert len(session.urls) == 2


def test_videos_for_selected_pages_deduplicates():
    session = FakeSession()
    scraper = JavtifulChannelScraper(session=session)
    info = scraper.inspect("https://javtiful.com/channel/madonna")
    urls = scraper.videos_for_pages(info, [1, 93, 1])
    assert len(urls) == 30
    assert len(set(urls)) == 30
