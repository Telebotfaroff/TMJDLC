from tmjdlc.providers.javtiful.stream_detector import (
    JavtifulStreamDetector,
    StreamResult,
    redact_stream_url,
)


def test_redact_stream_url_masks_signed_parameters():
    url = "https://cdn.example/hls/index.m3u8?v=abc&verify=secret&token=private"
    result = redact_stream_url(url)
    assert "secret" not in result
    assert "private" not in result
    assert "v=abc" in result
    assert "verify=%5BREDACTED%5D" in result


def test_choose_source_prefers_requested_quality():
    sources = [
        {"src": "https://cdn.example/480.m3u8", "size": 480},
        {"src": "https://cdn.example/720.m3u8", "size": 720},
    ]
    selected = JavtifulStreamDetector._choose_source(sources, 480)
    assert selected["size"] == 480


def test_choose_source_defaults_to_highest_quality():
    sources = [
        {"src": "https://cdn.example/480.m3u8", "size": 480},
        {"src": "https://cdn.example/1080.m3u8", "size": 1080},
    ]
    selected = JavtifulStreamDetector._choose_source(sources, None)
    assert selected["size"] == 1080


def test_stream_result_exposes_downloader_headers_without_logging_them():
    result = StreamResult(
        post_url="https://javtiful.com/video/113371/dvmm-444",
        stream_url="https://cdn.example/index.m3u8?verify=secret",
        format="hls",
        quality=720,
        label="720p",
        fallback_url="https://cdn.example/fallback",
        duration_seconds=120.0,
        headers={"Referer": "https://javtiful.com/", "User-Agent": "test-agent"},
    )
    assert result.to_ytdlp_options()["http_headers"]["Referer"].startswith("https://")
    assert "verify=secret" not in result.to_public_dict()["stream_url"]
    assert "Cookie" not in result.to_public_dict()
