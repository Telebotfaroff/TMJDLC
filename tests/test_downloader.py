import pytest

from tmjdlc.downloader import DownloadEngine


def test_job_id_validation_happens_before_directory_creation(tmp_path):
    engine = DownloadEngine(tmp_path)
    with pytest.raises(ValueError, match="job_id"):
        engine.download("https://javtiful.com/ABP-123", job_id="../escape")
    assert list(tmp_path.iterdir()) == []


def test_duplicate_job_directory_is_not_overwritten(tmp_path):
    engine = DownloadEngine(tmp_path)
    (tmp_path / "same-job").mkdir()
    with pytest.raises(FileExistsError):
        engine.download("https://javtiful.com/ABP-123", job_id="same-job")


def test_cleanup_rejects_traversal(tmp_path):
    engine = DownloadEngine(tmp_path)
    with pytest.raises(ValueError):
        engine.cleanup_job("../outside")
