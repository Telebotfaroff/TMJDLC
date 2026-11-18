from __future__ import annotations

import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tmjdlc.providers.javtiful import JavtifulProvider
from tmjdlc.providers.javtiful.provider import JavtifulURLParser

log = logging.getLogger(__name__)
ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class DownloadResult:
    job_id: str
    code: str
    title: str
    filename: str
    filepath: Path
    size: int
    duration: float | None
    thumbnail: str | None
    webpage_url: str
    extractor: str | None


class DownloadEngine:
    """Resolve a fresh Javtiful stream, then download it into one job directory."""

    def __init__(
        self,
        work_dir: str | Path,
        *,
        provider: JavtifulProvider | None = None,
        progress_callback: ProgressCallback | None = None,
        socket_timeout: int = 30,
        retries: int = 10,
        fragment_retries: int = 10,
    ):
        self.work_dir = Path(work_dir).expanduser()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.url_parser = JavtifulURLParser()
        self.progress_callback = progress_callback
        self.provider = provider or JavtifulProvider(
            progress_callback=self._provider_progress
        )
        self.socket_timeout = socket_timeout
        self.retries = retries
        self.fragment_retries = fragment_retries
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _provider_progress(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback({"status": "resolving", "message": message})

    def download(self, url: str, *, job_id: str | None = None) -> DownloadResult:
        source = self.url_parser.parse(url)
        identifier = job_id or uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
            raise ValueError("job_id may contain only letters, numbers, '_' and '-'")
        job_dir = self.work_dir / identifier
        job_dir.mkdir(parents=True, exist_ok=False)
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[identifier] = cancel_event

        def progress_hook(data: dict[str, Any]) -> None:
            if cancel_event.is_set():
                raise DownloadCancelled(f"Download {identifier} was cancelled")
            if self.progress_callback:
                payload = dict(data)
                payload["job_id"] = identifier
                self.progress_callback(payload)

        try:
            if cancel_event.is_set():
                raise DownloadCancelled(f"Download {identifier} was cancelled")
            filename = source.code or identifier
            filepath, stream = self.provider.download(
                source.url,
                job_dir,
                filename,
                progress_hook=progress_hook,
                concurrent_fragments=4,
            )
            if cancel_event.is_set():
                raise DownloadCancelled(f"Download {identifier} was cancelled")
            if not filepath.is_file() or filepath.stat().st_size <= 0:
                raise IOError("Javtiful downloader returned a missing or empty file")
            size = filepath.stat().st_size
            code = source.code or identifier.upper()
            return DownloadResult(
                job_id=identifier,
                code=code,
                title=code,
                filename=filepath.name,
                filepath=filepath,
                size=size,
                duration=stream.duration_seconds,
                thumbnail=None,
                webpage_url=source.url,
                extractor="JavtifulStreamDetector",
            )
        except Exception:
            log.exception("Download failed for job %s", identifier)
            raise
        finally:
            with self._lock:
                self._cancel_events.pop(identifier, None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_id)
            if not event:
                return False
            event.set()
            return True

    def cleanup_job(self, job_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
            raise ValueError("Invalid job ID")
        target = (self.work_dir / job_id).resolve()
        if target.parent != self.work_dir.resolve():
            raise ValueError("Refusing to clean a path outside the work directory")
        shutil.rmtree(target, ignore_errors=True)


class DownloadCancelled(RuntimeError):
    pass
