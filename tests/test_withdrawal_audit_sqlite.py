import importlib
import json

import pytest


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("WITHDRAWAL_AUDIT_PATH", str(tmp_path / "withdrawal_audit.json"))
    import app.config as config
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.withdrawal_audit as wa
    importlib.reload(wa)
    yield wa, tmp_path
    db.reset_for_tests()


def test_append_preserves_extra_fields(sqlite_env):
    wa, _ = sqlite_env
    wa.append_withdrawal_audit("wd-1", "Alice", 500.0, "2025-06-01T00:00:00",
                               "2025-06-02T00:00:00", "executed",
                               completed_at="2025-06-02T00:05:00")
    wa.append_withdrawal_audit("wd-2", "Bob", 250.0, "2025-06-01T00:00:00",
                               "2025-06-02T00:00:00", "failed",
                               reason="insufficient equity")
    entries = wa.load_withdrawal_audit()
    assert entries[0]["completed_at"] == "2025-06-02T00:05:00"
    assert entries[0]["status"] == "executed"
    assert entries[1]["reason"] == "insufficient equity"
    assert "extra_json" not in entries[0]  # stored column not leaked to callers


def test_exports_json(sqlite_env):
    wa, tmp_path = sqlite_env
    wa.append_withdrawal_audit("wd-3", "Carol", 100.0, "2025-06-01T00:00:00",
                               "2025-06-02T00:00:00", "canceled",
                               canceled_at="2025-06-01T01:00:00")
    exported = json.loads((tmp_path / "withdrawal_audit.json").read_text())
    assert exported["audit"][0]["id"] == "wd-3"
    assert exported["audit"][0]["canceled_at"] == "2025-06-01T01:00:00"


def test_data_readable_from_sqlite_without_json_file(sqlite_env):
    wa, tmp_path = sqlite_env
    wa.append_withdrawal_audit("wd-z", "Zoe", 100.0, "2025-06-01", "2025-06-02", "executed",
                               completed_at="2025-06-02T00:05:00")
    (tmp_path / "withdrawal_audit.json").unlink()   # remove JSON export
    entries = wa.load_withdrawal_audit()             # must load from DB
    assert entries[0]["id"] == "wd-z"
    assert entries[0]["completed_at"] == "2025-06-02T00:05:00"
