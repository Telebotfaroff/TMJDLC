from __future__ import annotations

from pathlib import Path

from tmjdlc.bot_queue import JobQueue


class FakeGitHubStore:
    def __init__(self):
        self.snapshots = 0
        self.upload_records = []

    def restore_database(self, destination):
        return False

    def save_database(self, source):
        assert Path(source).is_file()
        self.snapshots += 1

    def save_upload_record(self, record):
        self.upload_records.append(dict(record))
        return "uploads/test.json"


def test_sqlite_queue_lifecycle_and_live_process_listing(tmp_path):
    store = FakeGitHubStore()
    queue = JobQueue(store=store)
    queue.db_path = tmp_path / "tmjdlc.sqlite3"
    queue.initialize()

    job, created = queue.enqueue("12345", "https://javtiful.com/video/12345", 720)
    assert created is True

    claimed = queue.claim("download-1", 60, max_downloading=2, max_uploads=1, max_pending_uploads=1)
    assert claimed is not None
    assert claimed["id"] == job["id"]
    assert claimed["status"] == "downloading"

    assert queue.update_progress(job["id"], "download-1", "downloading", 42) is True
    assert queue.complete_stage(job["id"], "download-1", "download", {"size": 1024}) is True
    assert queue.set_status(job["id"], "download-1", "ready") is True

    live = queue.list_active_jobs()
    assert len(live) == 1
    assert live[0]["status"] == "ready"

    uploading = queue.claim_upload("upload-1", 60)
    assert uploading is not None
    assert uploading["status"] == "uploading"
    assert queue.set_status(job["id"], "upload-1", "completed") is True
    assert queue.counts()["completed"] == 1


def test_download_claim_obeys_active_download_limit(tmp_path):
    queue = JobQueue(store=FakeGitHubStore())
    queue.db_path = tmp_path / "tmjdlc.sqlite3"
    queue.initialize()
    for index in range(3):
        queue.enqueue(str(index), f"https://javtiful.com/video/{index}", 720)

    first = queue.claim("download-1", 60, max_downloading=2, max_uploads=1, max_pending_uploads=1)
    second = queue.claim("download-2", 60, max_downloading=2, max_uploads=1, max_pending_uploads=1)
    third = queue.claim("download-3", 60, max_downloading=2, max_uploads=1, max_pending_uploads=1)

    assert first is not None
    assert second is not None
    assert third is None


def test_completed_upload_record_is_saved_to_github_store(tmp_path):
    store = FakeGitHubStore()
    queue = JobQueue(store=store)
    queue.db_path = tmp_path / "tmjdlc.sqlite3"
    queue.initialize()
    record = {
        "job_id": 77,
        "video_id": "777",
        "source_url": "https://javtiful.com/video/777",
        "filename": "sample.mp4",
        "file_size": 4096,
        "duration_seconds": 12,
        "width": 1280,
        "height": 720,
        "upload_destination": "both",
        "telegram": {"parts": [{"chat_id": -1001, "message_id": 55, "file_id": "telegram-file"}]},
        "gofile": {"id": "gofile-file", "download_url": "https://gofile.io/d/example"},
        "uploaded_at": "2026-11-24T00:00:00Z",
    }

    saved_path = queue.save_upload_record(record)

    assert saved_path == "uploads/test.json"
    assert store.upload_records == [record]


def test_downloads_stop_when_one_video_is_waiting_for_single_uploader(tmp_path):
    queue = JobQueue(store=FakeGitHubStore())
    queue.db_path = tmp_path / "tmjdlc.sqlite3"
    queue.initialize()
    for index in range(3):
        queue.enqueue(str(index), f"https://javtiful.com/video/{index}", 720)

    first = queue.claim("download-1", 60, max_downloading=2, max_uploads=1, max_pending_uploads=1)
    assert first is not None
    assert queue.set_status(first["id"], "download-1", "ready") is True

    # One waiting upload already occupies the single-uploader backlog.
    assert queue.claim("download-2", 60, max_downloading=2, max_uploads=1, max_pending_uploads=1) is None

    uploading = queue.claim_upload("upload-1", 60)
    assert uploading is not None
    # The active uploader is full and there is no free ready slot.
    assert queue.claim("download-2", 60, max_downloading=2, max_uploads=1, max_pending_uploads=1) is None

def test_background_download_continues_while_upload_is_active(tmp_path):
    queue = JobQueue(store=FakeGitHubStore())
    queue.db_path = tmp_path / "tmjdlc.sqlite3"
    queue.initialize()
    first, _ = queue.enqueue("100", "https://javtiful.com/video/100", 720)
    second, _ = queue.enqueue("200", "https://javtiful.com/video/200", 720)

    downloading = queue.claim(
        "download-1", 60, max_downloading=1, max_uploads=1, max_pending_uploads=1
    )
    assert downloading is not None and downloading["id"] == first["id"]
    assert queue.set_status(first["id"], "download-1", "ready") is True
    uploading = queue.claim_upload("upload-1", 60)
    assert uploading is not None and uploading["id"] == first["id"]

    # Legacy mode waits for the active upload to finish.
    assert queue.claim(
        "download-2", 60, max_downloading=1, max_uploads=1, max_pending_uploads=1
    ) is None

    # Background mode keeps the next download running while upload 1 is active.
    next_job = queue.claim(
        "download-2", 60, max_downloading=1, max_uploads=1,
        max_pending_uploads=1, background_download=True,
    )
    assert next_job is not None
    assert next_job["id"] == second["id"]
    assert next_job["status"] == "downloading"


def test_background_download_still_respects_ready_backlog_limit(tmp_path):
    queue = JobQueue(store=FakeGitHubStore())
    queue.db_path = tmp_path / "tmjdlc.sqlite3"
    queue.initialize()
    first, _ = queue.enqueue("101", "https://javtiful.com/video/101", 720)
    queue.enqueue("202", "https://javtiful.com/video/202", 720)

    downloading = queue.claim(
        "download-1", 60, max_downloading=1, max_uploads=1,
        max_pending_uploads=1, background_download=True,
    )
    assert downloading is not None and downloading["id"] == first["id"]
    assert queue.set_status(first["id"], "download-1", "ready") is True

    # Prefetch does not grow without bound: one ready item fills this backlog.
    assert queue.claim(
        "download-2", 60, max_downloading=1, max_uploads=1,
        max_pending_uploads=1, background_download=True,
    ) is None



def test_manual_retry_preserves_completed_upload_stage_receipts(tmp_path):
    queue = JobQueue(store=FakeGitHubStore())
    queue.db_path = tmp_path / "tmjdlc.sqlite3"
    queue.initialize()
    job, _ = queue.enqueue("909", "https://javtiful.com/video/909", 720)
    claimed = queue.claim("download-1", 60)
    assert claimed is not None
    receipt = {
        "chat_id": -100123, "message_id": 456, "fallback": False,
        "parts": [{"chat_id": -100123, "message_id": 456, "file_id": "file-id"}],
        "part_count": 1,
    }
    assert queue.complete_stage(job["id"], "download-1", "video_upload", receipt) is True
    assert queue.set_status(job["id"], "download-1", "failed", error="simulated failure") is True

    retried, created = queue.enqueue(
        "909", "https://javtiful.com/video/909", 720, requested_by=42
    )

    assert created is True
    assert retried["status"] == "queued"
    assert queue.stage_done(job["id"], "video_upload") == receipt
