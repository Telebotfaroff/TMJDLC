"""Telegram media preparation helpers."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("tmjdlc.telegram_media")

# Keep every Telegram video part safely below the 2 GB upload ceiling.
# A small margin avoids boundary failures caused by multipart/container overhead.
TELEGRAM_MAX_FILE_BYTES = 1_950_000_000


def _fmt_bytes(value: float) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def prepare_telegram_parts(
    path: Path,
    video_id: str,
    *,
    max_file_bytes: int = TELEGRAM_MAX_FILE_BYTES,
) -> list[Path]:
    """Return the original file or split it into playable MP4 parts below Telegram's limit."""
    path = Path(path)
    max_file_bytes = min(TELEGRAM_MAX_FILE_BYTES, max(256 * 1024 * 1024, int(max_file_bytes)))
    if path.stat().st_size <= max_file_bytes:
        return [path]

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    duration = float((probe.stdout or "0").strip() or 0)
    if duration <= 0:
        raise RuntimeError("Cannot split oversized video: ffprobe could not read its duration.")

    pattern = path.parent / f"javtiful-{video_id}-part-%03d.mp4"
    target = max_file_bytes
    segment_seconds = max(10.0, duration * (target / path.stat().st_size) * 0.78)

    for attempt in range(1, 7):
        for old_part in path.parent.glob(f"javtiful-{video_id}-part-*.mp4"):
            old_part.unlink(missing_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(path), "-map", "0", "-c", "copy",
                "-f", "segment", "-segment_time", f"{segment_seconds:.3f}",
                "-reset_timestamps", "1", "-segment_format", "mp4",
                str(pattern),
            ],
            capture_output=True, check=True,
        )
        parts = sorted(path.parent.glob(f"javtiful-{video_id}-part-*.mp4"))
        if len(parts) < 2:
            raise RuntimeError(
                "FFmpeg could not split this video into multiple parts. "
                "The source may not contain suitable keyframes for stream-copy splitting."
            )
        oversized = [part for part in parts if part.stat().st_size > target]
        if not oversized:
            log.info(
                "Oversized video split | video_id=%s original_size=%s parts=%s max_part_size=%s",
                video_id, _fmt_bytes(path.stat().st_size), len(parts),
                _fmt_bytes(max(part.stat().st_size for part in parts)),
            )
            return parts
        segment_seconds *= 0.65
        log.warning(
            "Split attempt %s produced %s oversized part(s); retrying with shorter segments",
            attempt, len(oversized),
        )

    raise RuntimeError(
        "Could not create Telegram-safe video parts below "
        f"{_fmt_bytes(max_file_bytes)}."
    )
