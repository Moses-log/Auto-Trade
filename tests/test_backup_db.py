import base64
import importlib
import sqlite3

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


def _patch_gist(backup, monkeypatch, captured):
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


@pytest.mark.asyncio
async def test_db_included_in_payload(env, monkeypatch):
    backup, tmp_path = env
    captured = {}
    _patch_gist(backup, monkeypatch, captured)

    result = await backup.push_backup()
    assert result["ok"] is True
    assert "kimi.db.base64" in captured["files"]
    decoded = base64.b64decode(captured["files"]["kimi.db.base64"]["content"])
    assert decoded == b"SQLITE_FAKE_BINARY_\x00\x01\x02"


@pytest.mark.asyncio
async def test_backup_reflects_committed_data_through_wal(tmp_path, monkeypatch):
    """The pushed kimi.db.base64 must be a valid, complete SQLite db that
    includes rows committed just before push_backup() runs — proving the WAL
    is checkpointed before the raw bytes are read, not stale/incomplete."""
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))

    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    with db.writer() as conn:
        conn.execute("INSERT INTO investors(name) VALUES('Alice')")

    import app.config as config
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.backup as backup
    importlib.reload(backup)

    captured = {}
    _patch_gist(backup, monkeypatch, captured)

    checkpoint_calls = []
    real_checkpoint = db.checkpoint
    monkeypatch.setattr(db, "checkpoint", lambda: (checkpoint_calls.append(1), real_checkpoint())[1])

    result = await backup.push_backup()
    assert result["ok"] is True
    assert checkpoint_calls, "db.checkpoint() must be called before reading kimi.db bytes"
    assert "kimi.db.base64" in captured["files"]

    decoded = base64.b64decode(captured["files"]["kimi.db.base64"]["content"])
    restored_path = tmp_path / "restored_kimi.db"
    restored_path.write_bytes(decoded)

    conn = sqlite3.connect(str(restored_path))
    try:
        rows = conn.execute("SELECT name FROM investors").fetchall()
    finally:
        conn.close()
    assert rows == [("Alice",)]

    db.reset_for_tests()
