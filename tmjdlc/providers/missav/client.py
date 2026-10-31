"""MissAV provider backed by the unofficial async API package."""

from __future__ import annotations

from types import TracebackType
from typing import Any

from base_api import BaseCore, DownloadConfigHLS
from base_api.modules.config import RuntimeConfig
from missav_api import Client, Video


class MissAVProvider:
    """Async context-managed client for MissAV metadata and HLS downloads."""

    def __init__(self, runtime_config: RuntimeConfig | None = None) -> None:
        self.runtime_config = runtime_config or RuntimeConfig()
        self._core_context: Any | None = None
        self._core: BaseCore | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> MissAVProvider:
        self._core_context = BaseCore(self.runtime_config)
        self._core = await self._core_context.__aenter__()
        self._client = Client(core=self._core)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._core_context is not None:
            return await self._core_context.__aexit__(exc_type, exc, traceback)
        return None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Use MissAVProvider inside 'async with' first.")
        return self._client

    async def get_video(self, url: str, *, load_html: bool = True) -> Video:
        """Fetch a MissAV video object and optionally load its HTML metadata."""
        return await self.client.get_video(url, load_html=load_html)

    async def search(self, query: str, *, video_count: int = 50):
        """Yield search results as ScrapeResult[Video] objects."""
        async for result in self.client.search(query, video_count=video_count):
            yield result

    async def download(
        self,
        video: Video,
        output_dir: str,
        *,
        quality: str = "best",
    ):
        """Download a fetched video using the API's native HLS downloader."""
        if quality not in {"best", "half", "worst"}:
            if not quality.isdigit():
                raise ValueError("quality must be best, half, worst, or a resolution")
            if int(quality) < 1:
                raise ValueError("numeric quality must be positive")
        config = DownloadConfigHLS(
            quality=quality,
            path=output_dir,
            no_title=True,
        )
        return await video.download(config)

    @staticmethod
    def metadata(video: Video) -> dict[str, str | None]:
        """Return stable, serializable metadata without stream URLs."""
        return {
            "url": video.url,
            "title": video.title,
            "publish_date": video.publish_date,
            "keywords": video.keywords,
            "length": video.length,
            "thumbnail": video.thumbnail,
        }
