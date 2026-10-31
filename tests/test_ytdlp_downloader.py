from tmjdlc.downloaders import YtDlpStreamDownloader
from tmjdlc.providers.javtiful.stream_detector import StreamResult


def test_ytdlp_options_use_resolved_headers_and_fragment_concurrency(tmp_path):
    stream = StreamResult(
        post_url="https://javtiful.com/video/113371/dvmm-444",
        stream_url="https://cdn.example/index.m3u8?verify=secret",
        format="hls",
        quality=720,
        label="720p",
        fallback_url=None,
        duration_seconds=120,
        headers={"Referer": "https://javtiful.com/", "User-Agent": "test-agent"},
    )
    options = YtDlpStreamDownloader.build_options(
        stream, tmp_path, "DVMM-444", concurrent_fragments=3
    )
    assert options["http_headers"]["Referer"] == "https://javtiful.com/"
    assert options["concurrent_fragment_downloads"] == 3
    assert options["noplaylist"] is True
    assert options["outtmpl"].endswith("DVMM-444.%(ext)s")


def test_ytdlp_options_reject_unsafe_filename(tmp_path):
    stream = StreamResult(
        post_url="https://javtiful.com/video/113371/dvmm-444",
        stream_url="https://cdn.example/index.m3u8",
        format="hls",
        quality=720,
        label="720p",
        fallback_url=None,
        duration_seconds=None,
        headers={},
    )
    try:
        YtDlpStreamDownloader.build_options(stream, tmp_path, "../escape")
    except ValueError as exc:
        assert "plain file name" in str(exc)
    else:
        raise AssertionError("unsafe filename should be rejected")
