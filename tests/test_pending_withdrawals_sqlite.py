import importlib
import json

import pytest


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("PENDING_WITHDRAWALS_PATH", str(tmp_path / "pending_withdrawals.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.pending_withdrawals as pw
    importlib.reload(pw)
    yield pw, tmp_path
    db.reset_for_tests()


def test_save_get_remove_roundtrip(sqlite_env):
    pw, _ = sqlite_env
    pw.save_pending_withdrawal("wd-1", "Alice", 500.0, "2025-06-01T00:00:00", "2025-06-02T00:00:00")
    pw.save_pending_withdrawal("wd-2", "Bob", 250.0, "2025-06-01T00:00:00", "2025-06-02T00:00:00",
                               spy_price=540.0)
    assert pw.get_pending_withdrawal("wd-1")["investor"] == "Alice"
    assert pw.get_pending_withdrawal("wd-2")["spy_price"] == 540.0
    assert "spy_price" not in pw.get_pending_withdrawal("wd-1")  # None omitted, matches JSON shape
    pw.remove_pending_withdrawal("wd-1")
    assert pw.get_pending_withdrawal("wd-1") is None
    assert {r["id"] for r in pw.load_pending_withdrawals()} == {"wd-2"}


def test_exports_json(sqlite_env):
    pw, tmp_path = sqlite_env
    pw.save_pending_withdrawal("wd-9", "Carol", 100.0, "2025-06-01T00:00:00", "2025-06-02T00:00:00")
    exported = json.loads((tmp_path / "pending_withdrawals.json").read_text())
    assert exported["pending"][0]["id"] == "wd-9"


def test_data_readable_from_sqlite_without_json_file(sqlite_env):
    pw, tmp_path = sqlite_env
    pw.save_pending_withdrawal("wd-x", "Zoe", 100.0, "2025-06-01", "2025-06-02")
    (tmp_path / "pending_withdrawals.json").unlink()   # remove JSON export
    assert pw.get_pending_withdrawal("wd-x")["investor"] == "Zoe"  # must load from DB
