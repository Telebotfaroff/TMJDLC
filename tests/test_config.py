import pytest

from tmjdlc.config import Settings


def test_settings_parse(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TG_ADMIN_IDS", "123, 456")
    monkeypatch.setenv("TMJDLC_WORK_DIR", str(tmp_path))
    settings = Settings.from_env()
    assert settings.admin_ids == frozenset({123, 456})
    assert settings.max_parallel_downloads == 2
    assert settings.work_dir == tmp_path


def test_missing_token_is_rejected(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TG_ADMIN_IDS", "123")
    with pytest.raises(ValueError, match="TG_BOT_TOKEN"):
        Settings.from_env()


def test_parallelism_is_bounded(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "x")
    monkeypatch.setenv("TG_ADMIN_IDS", "123")
    monkeypatch.setenv("TMJDLC_MAX_PARALLEL_DOWNLOADS", "99")
    with pytest.raises(ValueError, match="between 1 and 6"):
        Settings.from_env()
