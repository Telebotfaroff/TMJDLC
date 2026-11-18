from __future__ import annotations

import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from yt_dlp import YoutubeDL

from tmjdlc.providers.javtiful import JavtifulProvider

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
    """One-job yt-dlp engine. Queueing and worker limits live outside this class."""

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
        self.provider = provider or JavtifulProvider()
        self.progress_callback = progress_callback
        self.socket_timeout = socket_timeout
        self.retries = retries
        self.fragment_retries = fragment_retries
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def download(self, url: str, *, job_id: str | None = None) -> DownloadResult:
        source = self.provider.parse(url)
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
            status = data.get("status")
            payload: dict[str, Any] = {"job_id": identifier, "status": status}
            if status == "downloading":
                payload.update({
                    "downloaded_bytes": data.get("downloaded_bytes", 0),
                    "total_bytes": data.get("total_bytes") or data.get("total_bytes_estimate"),
                    "speed": data.get("speed"),
                    "eta": data.get("eta"),
                    "filename": data.get("filename"),
                })
            elif status == "finished":
                payload["filename"] = data.get("filename")
            if self.progress_callback:
                self.progress_callback(payload)

        options = {
            "outtmpl": str(job_dir / "%(title).150B [%(id)s].%(ext)s"),
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "continuedl": True,
            "overwrites": False,
            "retries": self.retries,
            "fragment_retries": self.fragment_retries,
            "file_access_retries": 3,
            "socket_timeout": self.socket_timeout,
            "concurrent_fragment_downloads": 4,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "windowsfilenames": True,
            "progress_hooks": [progress_hook],
            "logger": _YtDlpLogger(),
        }

        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(source.url, download=True)
                if not isinstance(info, dict):
                    raise RuntimeError("yt-dlp returned no media metadata")
                requested = info.get("requested_downloads") or []
                candidates = [item.get("filepath") for item in requested if item.get("filepath")]
                candidates.extend([info.get("_filename"), info.get("filepath")])
                filepath = next((Path(p) for p in candidates if p and Path(p).is_file()), None)
                if filepath is None:
                    files = [p for p in job_dir.rglob("*") if p.is_file() and p.stat().st_size > 0]
                    if len(files) == 1:
                        filepath = files[0]
                    else:
                        raise FileNotFoundError("yt-dlp finished but the final media file could not be identified")
                size = filepath.stat().st_size
                if size <= 0:
                    raise IOError("Downloaded media file is empty")
                code = source.code or self.provider.extract_code(str(info.get("title", "")))
                code = code or str(info.get("id") or identifier).upper()
                return DownloadResult(
                    job_id=identifier,
                    code=code,
                    title=str(info.get("title") or filepath.stem),
                    filename=filepath.name,
                    filepath=filepath,
                    size=size,
                    duration=info.get("duration"),
                    thumbnail=info.get("thumbnail"),
                    webpage_url=str(info.get("webpage_url") or source.url),
                    extractor=info.get("extractor_key") or info.get("extractor"),
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


class _YtDlpLogger:
    def debug(self, message: str) -> None:
        if message.startswith("[debug]"):
            log.debug(message)

    def info(self, message: str) -> None:
        log.info(message)

    def warning(self, message: str) -> None:
        log.warning(message)

    def error(self, message: str) -> None:
        log.error(message)
