from pathlib import Path
from types import SimpleNamespace

import pytest

from tmjdlc import telegram_media
from tmjdlc.telegram_media import TELEGRAM_MAX_FILE_BYTES


def test_small_video_is_not_split(tmp_path):
    source = tmp_path / "small.mp4"
    source.write_bytes(b"small")
    assert telegram_media.prepare_telegram_parts(source, "abc", max_file_bytes=10) == [source]


def test_oversized_video_is_split_and_every_part_is_below_limit(tmp_path, monkeypatch):
    source = tmp_path / "large.mp4"
    source.write_bytes(b"x" * 250)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "ffprobe":
            return SimpleNamespace(stdout="120")
        pattern = command[-1]
        for index, size in enumerate((90, 80, 80), start=1):
            Path(pattern.replace("%03d", f"{index:03d}")).write_bytes(b"x" * size)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(telegram_media.subprocess, "run", fake_run)
    parts = telegram_media.prepare_telegram_parts(source, "abc", max_file_bytes=100)

    assert len(parts) == 3
    assert all(part.stat().st_size <= 100 for part in parts)
    assert calls[0][0] == "ffprobe"
    assert calls[1][0] == "ffmpeg"
    assert "-segment_format" in calls[1]


def test_splitter_never_accepts_a_limit_above_telegram_ceiling(tmp_path):
    source = tmp_path / "within_real_limit.mp4"
    source.write_bytes(b"small")
    assert telegram_media.prepare_telegram_parts(
        source, "abc", max_file_bytes=TELEGRAM_MAX_FILE_BYTES * 2
    ) == [source]
