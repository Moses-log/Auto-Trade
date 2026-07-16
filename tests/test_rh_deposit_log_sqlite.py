import importlib
import json

import pytest


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("RH_DEPOSIT_LOG_PATH", str(tmp_path / "rh_deposits.json"))
    import app.config as config
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.rh_deposit_log as rd
    importlib.reload(rd)
    yield rd, tmp_path
    db.reset_for_tests()


def test_append_sorts_and_roundtrips(sqlite_env):
    rd, _ = sqlite_env
    rd.append_rh_deposit("2025-03-01", 300.0)
    rd.append_rh_deposit("2025-01-01", 100.0)
    rd.append_rh_deposit("2025-02-01", 200.0)
    assert rd.get_rh_deposit_events() == [
        ("2025-01-01", 100.0), ("2025-02-01", 200.0), ("2025-03-01", 300.0)]


def test_clear(sqlite_env):
    rd, _ = sqlite_env
    rd.append_rh_deposit("2025-01-01", 100.0)
    assert rd.clear_rh_deposits() == 1
    assert rd.load_rh_deposits() == []


def test_exports_json(sqlite_env):
    rd, tmp_path = sqlite_env
    rd.append_rh_deposit("2025-01-01", 100.0)
    exported = json.loads((tmp_path / "rh_deposits.json").read_text())
    assert exported == [{"date": "2025-01-01", "amount": 100.0}]


def test_data_readable_from_sqlite_without_json_file(sqlite_env):
    rd, tmp_path = sqlite_env
    rd.append_rh_deposit("2025-01-01", 100.0)
    (tmp_path / "rh_deposits.json").unlink()   # remove JSON export
    assert rd.load_rh_deposits() == [{"date": "2025-01-01", "amount": 100.0}]  # from DB
