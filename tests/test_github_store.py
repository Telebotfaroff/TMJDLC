from __future__ import annotations

import base64

from tmjdlc.github_store import GitHubDataStore, GitHubStoreError


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.payload = payload or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.files = {}

    def get(self, url, params=None, timeout=30):
        path = url.split("/contents/", 1)[-1]
        if path not in self.files:
            return FakeResponse(404)
        raw = self.files[path]
        return FakeResponse(200, {
            "type": "file",
            "sha": "blob-sha",
            "content": base64.b64encode(raw).decode("ascii"),
        })

    def put(self, url, json, timeout=30):
        path = url.split("/contents/", 1)[-1]
        self.files[path] = base64.b64decode(json["content"])
        return FakeResponse(201, {"content": {"sha": "new-blob-sha"}})


def test_github_json_store_round_trip():
    session = FakeSession()
    store = GitHubDataStore("token", session=session)

    store.write_json("config/bot.json", {"download_workers": 2}, "test config")
    assert store.read_json("config/bot.json") == {"download_workers": 2}


def test_github_store_requires_token():
    try:
        GitHubDataStore("")
    except GitHubStoreError as exc:
        assert "GITHUB_TOKEN" in str(exc)
    else:
        raise AssertionError("Expected missing token error")
