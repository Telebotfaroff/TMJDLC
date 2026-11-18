import json

from tmjdlc.storage import JsonStore


def test_missing_database_starts_empty(tmp_path):
    store = JsonStore(tmp_path / "database.json")
    assert store.read() == {"version": 1, "videos": {}, "jobs": {}}


def test_record_and_duplicate_lookup(tmp_path):
    store = JsonStore(tmp_path / "database.json")
    store.record_video("abp-123", folder_id="drive-id", folder_url="https://drive/folder",
                       filename="video.mp4", size=1234)
    assert store.contains_code("ABP-123")
    assert not store.contains_code("ABP-999")
    data = json.loads((tmp_path / "database.json").read_text())
    assert data["videos"]["ABP-123"]["size"] == 1234


def test_update_preserves_existing_entries(tmp_path):
    store = JsonStore(tmp_path / "database.json")
    store.record_video("AAA-001", folder_id="1", folder_url="u1", filename="a.mp4", size=10)
    store.record_video("BBB-002", folder_id="2", folder_url="u2", filename="b.mp4", size=20)
    assert set(store.read()["videos"]) == {"AAA-001", "BBB-002"}


def test_corrupt_primary_recovers_from_backup(tmp_path):
    store = JsonStore(tmp_path / "database.json")
    store.record_video("AAA-001", folder_id="1", folder_url="u1", filename="a.mp4", size=10)
    store.record_video("BBB-002", folder_id="2", folder_url="u2", filename="b.mp4", size=20)
    store.path.write_text("{broken", encoding="utf-8")
    assert set(store.read()["videos"]) == {"AAA-001"}
