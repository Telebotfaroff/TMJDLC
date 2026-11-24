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
