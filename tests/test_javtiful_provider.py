from pathlib import Path

from tmjdlc.models.stream import StreamResult
from tmjdlc.providers.javtiful.client import JavtifulProvider


class FakeDetector:
    def __init__(self):
        self.calls = 0

    def detect(self, post_url, *, preferred_quality=None):
        self.calls += 1
        return StreamResult(
            post_url=post_url,
            stream_url=f"https://media.example/{self.calls}.mp4?signature=temporary",
            format="mp4",
            quality=720,
            label="720p",
            fallback_url=None,
            duration_seconds=60,
        )


class FakeDownloader:
    def __init__(self, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    def download(self, stream, output_dir, filename, *, progress_hook=None, concurrent_fragments=4):
        self.calls.append((stream.stream_url, filename))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("expired")
        path = Path(output_dir) / f"{filename}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return path


def test_provider_resolves_immediately_before_download(tmp_path):
    detector = FakeDetector()
    downloader = FakeDownloader()
    provider = JavtifulProvider(detector=detector, downloader=downloader)

    path, stream = provider.download(
        "https://javtiful.com/video/115269/example",
        tmp_path,
        "test-video",
        quality=720,
    )

    assert path.exists()
    assert detector.calls == 1
    assert downloader.calls[0][0] == stream.stream_url


def test_provider_refreshes_url_after_failed_download(tmp_path):
    detector = FakeDetector()
    downloader = FakeDownloader(fail_first=True)
    provider = JavtifulProvider(
        detector=detector, downloader=downloader, max_attempts=2
    )

    path, stream = provider.download(
        "https://javtiful.com/video/115269/example",
        tmp_path,
        "test-video",
        quality=720,
    )

    assert path.exists()
    assert detector.calls == 2
    assert downloader.calls[0][0] != downloader.calls[1][0]
    assert downloader.calls[0][1] == "test-video"
    assert downloader.calls[1][1] == "test-video.retry2"
    assert stream.stream_url == downloader.calls[1][0]
