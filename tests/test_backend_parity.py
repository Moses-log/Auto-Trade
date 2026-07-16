import importlib

import pytest


def _fixture_investors(investors_mod):
    return [
        investors_mod.Investor(name="Alice",
            deposits=[investors_mod.Deposit(1000.0, 500.0, "2025-01-01"),
                      investors_mod.Deposit(500.0, 520.0, "2025-03-01")],
            withdrawals=[investors_mod.Withdrawal(0.4, 540.0, 200.0, 216.0, "2025-06-01")]),
        investors_mod.Investor(name="Bob",
            deposits=[investors_mod.Deposit(2000.0, 510.0, "2025-02-01")]),
    ]


def test_breakdown_identical_across_backends(tmp_path, monkeypatch):
    # JSON backend
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    import app.config as config
    monkeypatch.setattr(config.settings, "use_sqlite", False)
    import app.investors as inv_json
    importlib.reload(inv_json)
    data = _fixture_investors(inv_json)
    inv_json.save_investors(data)
    json_break = inv_json.format_discord_message(
        inv_json.compute_breakdown(inv_json.load_investors(), 500.0, 1_000_000.0), "test")

    # SQLite backend
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.investors as inv_sql
    importlib.reload(inv_sql)
    inv_sql.save_investors(_fixture_investors(inv_sql))
    sql_break = inv_sql.format_discord_message(
        inv_sql.compute_breakdown(inv_sql.load_investors(), 500.0, 1_000_000.0), "test")

    assert json_break == sql_break
    db.reset_for_tests()
