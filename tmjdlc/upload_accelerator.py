"""Optional parallel MTProto upload acceleration with a native-client fallback."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("tmjdlc.upload")

try:
    from pyrospeed import PyroSpeed, TransferConfig, UploadConfig
except ImportError:
    PyroSpeed = TransferConfig = UploadConfig = None

_ENABLED = os.getenv("TMJDLC_PYROSPEED_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
_CONNECTIONS = max(2, min(8, int(os.getenv("TMJDLC_PYROSPEED_CONNECTIONS", "4"))))
_INFLIGHT = max(1, min(4, int(os.getenv("TMJDLC_PYROSPEED_INFLIGHT", "2"))))


class UploadAccelerator:
    """Owns the optional accelerator lifecycle for one already-started client."""

    def __init__(self, client):
        self.client = client
        self._accelerator = None

    @property
    def active(self) -> bool:
        return self._accelerator is not None

    def install(self) -> bool:
        if not _ENABLED:
            log.info("Parallel upload acceleration disabled; using native client")
            return False
        if PyroSpeed is None:
            log.warning("Optional upload accelerator is unavailable; using native client")
            return False
        try:
            config = TransferConfig(
                upload=UploadConfig(
                    connections=_CONNECTIONS,
                    inflight_per_connection=_INFLIGHT,
                    queue_factor=2,
                    progress_interval=0.5,
                    disk_io="thread",
                    small_file_strategy="native",
                    max_parallel_files=1,
                )
            )
            accelerator = PyroSpeed(self.client, config)
            accelerator.install()
            self._accelerator = accelerator
            log.info(
                "Parallel upload acceleration enabled | connections=%s inflight=%s",
                _CONNECTIONS, _INFLIGHT,
            )
            return True
        except Exception:
            log.exception("Could not initialize parallel uploader; using native client")
            self._accelerator = None
            return False

    def disable(self, reason: str) -> None:
        accelerator, self._accelerator = self._accelerator, None
        if accelerator is not None:
            try:
                accelerator.uninstall()
            except Exception:
                log.exception("Could not restore native uploader cleanly")
            log.warning("Parallel uploader disabled (%s); future retries use native client", reason)
