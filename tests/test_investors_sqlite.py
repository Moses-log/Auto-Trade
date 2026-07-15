import importlib
import json

import pytest


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.investors as investors
    importlib.reload(investors)
    yield investors, tmp_path
    db.reset_for_tests()


def test_roundtrip_returns_identical_dataclasses(sqlite_env):
    investors, _ = sqlite_env
    orig = [
        investors.Investor(
            name="Alice",
            deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-02"),
                      investors.Deposit(amount=250.0, entry_spy=520.0, date="2025-03-01")],
            withdrawals=[investors.Withdrawal(units=0.5, exit_spy=540.0,
                         cost_basis=250.0, proceeds=270.0, date="2025-06-01")],
        ),
        investors.Investor(name="Bob",
            deposits=[investors.Deposit(amount=2000.0, entry_spy=510.0, date="2025-02-01")]),
    ]
    investors.save_investors(orig)
    loaded = investors.load_investors()
    assert investors.serialize_investors(loaded) == investors.serialize_investors(orig)

    from app import db
    assert db.get_conn().execute("SELECT COUNT(*) FROM deposits").fetchone()[0] == 3
    assert db.get_conn().execute("SELECT COUNT(*) FROM withdrawals").fetchone()[0] == 1


def test_deposit_order_preserved(sqlite_env):
    investors, _ = sqlite_env
    inv = investors.Investor(name="Carol", deposits=[
        investors.Deposit(amount=1.0, entry_spy=100.0, date="2025-01-01"),
        investors.Deposit(amount=2.0, entry_spy=101.0, date="2025-01-02"),
        investors.Deposit(amount=3.0, entry_spy=102.0, date="2025-01-03"),
    ])
    investors.save_investors([inv])
    loaded = investors.load_investors()[0]
    assert [d.amount for d in loaded.deposits] == [1.0, 2.0, 3.0]


def test_data_readable_from_sqlite_without_json_file(sqlite_env):
    investors, tmp_path = sqlite_env
    investors.save_investors([investors.Investor(
        name="Eve", deposits=[investors.Deposit(amount=1.0, entry_spy=100.0, date="2025-01-01")])])
    (tmp_path / "investors.json").unlink()   # remove the JSON export entirely
    reloaded = investors.load_investors()     # must still work — DB is the source of truth
    assert reloaded[0].name == "Eve"
    assert reloaded[0].deposits[0].amount == 1.0


def test_write_exports_json_snapshot(sqlite_env):
    investors, tmp_path = sqlite_env
    investors.save_investors([investors.Investor(name="Dave",
        deposits=[investors.Deposit(amount=5.0, entry_spy=500.0, date="2025-01-01")])])
    exported = json.loads((tmp_path / "investors.json").read_text())
    assert exported["investors"][0]["name"] == "Dave"
    assert exported["investors"][0]["deposits"][0]["amount"] == 5.0
