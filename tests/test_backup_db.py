import base64
import importlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    import app.config as config
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    (tmp_path / "kimi.db").write_bytes(b"SQLITE_FAKE_BINARY_\x00\x01\x02")
    import app.backup as backup
    importlib.reload(backup)
    yield backup, tmp_path


@pytest.mark.asyncio
async def test_db_included_in_payload(env, monkeypatch):
    backup, tmp_path = env
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def patch(self, url, json, headers):
            captured["files"] = json["files"]
            return FakeResp()

    monkeypatch.setattr(backup.settings, "github_gist_token", "t")
    monkeypatch.setattr(backup.settings, "github_gist_id", "g")
    monkeypatch.setattr(backup.httpx, "AsyncClient", FakeClient)

    result = await backup.push_backup()
    assert result["ok"] is True
    assert "kimi.db.base64" in captured["files"]
    decoded = base64.b64decode(captured["files"]["kimi.db.base64"]["content"])
    assert decoded == b"SQLITE_FAKE_BINARY_\x00\x01\x02"
