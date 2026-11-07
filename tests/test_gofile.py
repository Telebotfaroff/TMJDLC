from __future__ import annotations

from pathlib import Path

from tmjdlc import gofile


class FakeQueue:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeResponse:
    ok = True
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return {"status": "ok", "data": self.data}


def test_guest_account_and_folder_are_created_once(monkeypatch):
    queue = FakeQueue()
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/accounts"):
            return FakeResponse({"token": "guest-secret", "rootFolder": "root-uuid"})
        return FakeResponse({"id": "folder-uuid", "code": "folderCode"})

    monkeypatch.setattr(gofile.requests, "post", fake_post)

    first = gofile.ensure_guest_folder(queue)
    second = gofile.ensure_guest_folder(queue)

    assert first == second
    assert len(calls) == 2
    assert queue.settings["gofile_guest_token"] == "guest-secret"
    assert queue.settings["gofile_folder_id"] == "folder-uuid"
    assert queue.settings["gofile_folder_url"] == "https://gofile.io/d/folderCode"


def test_upload_uses_saved_guest_and_shared_folder(monkeypatch, tmp_path: Path):
    queue = FakeQueue({
        "gofile_guest_token": "guest-secret",
        "gofile_root_folder": "root-uuid",
        "gofile_folder_id": "folder-uuid",
        "gofile_folder_code": "folderCode",
        "gofile_folder_url": "https://gofile.io/d/folderCode",
    })
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video-bytes")
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["data"] = kwargs["data"]
        return FakeResponse({
            "id": "file-uuid",
            "name": "sample.mp4",
            "size": video.stat().st_size,
            "code": "fileCode",
            "downloadPage": "https://gofile.io/d/fileCode",
        })

    monkeypatch.setattr(gofile.requests, "post", fake_post)

    result = gofile.upload_file(queue, video)

    assert captured["url"] == gofile.UPLOAD_URL
    assert captured["headers"]["Authorization"] == "Bearer guest-secret"
    assert "multipart/form-data" in captured["headers"]["Content-Type"]
    assert result["folder_id"] == "folder-uuid"
    assert result["download_url"] == "https://gofile.io/d/fileCode"
    assert result["folder_url"] == "https://gofile.io/d/folderCode"



def test_twentieth_video_closes_folder_and_creates_next_public_folder(monkeypatch, tmp_path: Path):
    queue = FakeQueue({
        "gofile_guest_token": "guest-secret",
        "gofile_root_folder": "root-uuid",
        "gofile_folder_id": "folder-20",
        "gofile_folder_code": "folderCode20",
        "gofile_folder_url": "https://gofile.io/d/folderCode20",
        "gofile_folder_count": "19",
        "gofile_folder_number": "3",
    })
    video = tmp_path / "twentieth.mp4"
    video.write_bytes(b"video-bytes")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == gofile.UPLOAD_URL:
            return FakeResponse({
                "id": "file-20", "name": video.name, "size": video.stat().st_size,
                "code": "fileCode20", "downloadPage": "https://gofile.io/d/fileCode20",
            })
        return FakeResponse({"id": "folder-21", "code": "folderCode21"})

    monkeypatch.setattr(gofile.requests, "post", fake_post)
    receipt = gofile.upload_file(queue, video)

    assert receipt["folder_completed"]["folder_url"] == "https://gofile.io/d/folderCode20"
    assert receipt["folder_completed"]["video_count"] == 20
    assert queue.settings["gofile_folder_id"] == "folder-21"
    assert queue.settings["gofile_folder_url"] == "https://gofile.io/d/folderCode21"
    assert queue.settings["gofile_folder_count"] == "0"
    assert queue.settings["gofile_folder_number"] == "4"
    folder_call = next(call for call in calls if call[0].endswith("/contents/createFolder"))
    assert folder_call[1]["json"]["public"] is True


def test_finalize_partial_folder_returns_link_and_starts_next_folder(monkeypatch):
    queue = FakeQueue({
        "gofile_guest_token": "guest-secret",
        "gofile_root_folder": "root-uuid",
        "gofile_folder_id": "folder-partial",
        "gofile_folder_code": "partialCode",
        "gofile_folder_url": "https://gofile.io/d/partialCode",
        "gofile_folder_count": "7",
        "gofile_folder_number": "2",
    })

    def fake_post(url, **kwargs):
        return FakeResponse({"id": "folder-next", "code": "nextCode"})

    monkeypatch.setattr(gofile.requests, "post", fake_post)
    result = gofile.finalize_current_folder(queue)

    assert result["video_count"] == 7
    assert result["folder_url"] == "https://gofile.io/d/partialCode"
    assert queue.settings["gofile_folder_id"] == "folder-next"
    assert queue.settings["gofile_folder_count"] == "0"
    assert queue.settings["gofile_folder_number"] == "3"
