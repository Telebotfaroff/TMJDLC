from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _positive_int(name: str, default: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    work_dir: Path
    drive_root_name: str
    drive_root_id: str | None
    max_parallel_downloads: int
    max_parallel_uploads: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TG_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TG_BOT_TOKEN is required")
        raw_admins = os.getenv("TG_ADMIN_IDS", "").strip()
        try:
            admins = frozenset(int(item.strip()) for item in raw_admins.split(",") if item.strip())
        except ValueError as exc:
            raise ValueError("TG_ADMIN_IDS must be comma-separated integers") from exc
        if not admins:
            raise ValueError("At least one TG_ADMIN_IDS entry is required")

        return cls(
            bot_token=token,
            admin_ids=admins,
            work_dir=Path(os.getenv("TMJDLC_WORK_DIR", "/content/TMJDLC-temp")).expanduser(),
            drive_root_name=os.getenv("TMJDLC_DRIVE_ROOT_NAME", "TMJDLC Library").strip(),
            drive_root_id=os.getenv("TMJDLC_DRIVE_ROOT_ID", "").strip() or None,
            max_parallel_downloads=_positive_int("TMJDLC_MAX_PARALLEL_DOWNLOADS", 2, 6),
            max_parallel_uploads=_positive_int("TMJDLC_MAX_PARALLEL_UPLOADS", 1, 4),
            log_level=os.getenv("TMJDLC_LOG_LEVEL", "INFO").upper(),
        )
