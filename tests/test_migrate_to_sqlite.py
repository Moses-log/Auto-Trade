import importlib
import json

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # JSON sources (flag OFF for reads), DB target
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    monkeypatch.setenv("PENDING_WITHDRAWALS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setenv("WITHDRAWAL_AUDIT_PATH", str(tmp_path / "audit.json"))
    monkeypatch.setenv("RH_DEPOSIT_LOG_PATH", str(tmp_path / "rh_deposits.json"))
    monkeypatch.setenv("PRE_SQLITE_BACKUP_DIR", str(tmp_path / "backup_pre_sqlite"))

    # Reset shared db state (connection + exporter registry) BEFORE this
    # test runs, so leftover exporters registered by a previous test (bound
    # to that test's tmp paths) can't fire against this test's db/paths.
    import app.db as db
    db.reset_for_tests()

    # Reload in dependency order: config (env vars) -> db (fresh globals,
    # already reset above) -> store modules (re-register exporters against
    # the freshly-reset db and this test's tmp paths).
    import app.config as config
    importlib.reload(config)
    importlib.reload(db)

    import app.investors as investors
    import app.pending_withdrawals as pending_withdrawals
    import app.withdrawal_audit as withdrawal_audit
    import app.rh_deposit_log as rh_deposit_log
    importlib.reload(investors)
    importlib.reload(pending_withdrawals)
    importlib.reload(withdrawal_audit)
    importlib.reload(rh_deposit_log)

    yield config, tmp_path

    db.reset_for_tests()


def _seed_json(tmp_path):
    (tmp_path / "investors.json").write_text(json.dumps({"investors": [
        {"name": "Alice", "deposits": [
            {"amount": 1000.0, "entry_spy": 500.0, "date": "2025-01-01"},
            {"amount": 500.0, "entry_spy": 520.0, "date": "2025-03-01"}],
         "withdrawals": [
            {"units": 0.4, "exit_spy": 540.0, "cost_basis": 200.0,
             "proceeds": 216.0, "date": "2025-06-01"}]},
        {"name": "Bob", "deposits": [
            {"amount": 2000.0, "entry_spy": 510.0, "date": "2025-02-01"}],
         "withdrawals": []},
    ]}))
    (tmp_path / "pending.json").write_text(json.dumps({"pending": [
        {"id": "wd-1", "investor": "Alice", "amount": 100.0,
         "requested_at": "2025-06-10", "run_at": "2025-06-11", "spy_price": 545.0}]}))
    (tmp_path / "audit.json").write_text(json.dumps({"audit": [
        {"id": "wd-0", "investor": "Alice", "amount": 216.0, "requested_at": "2025-05-31",
         "run_at": "2025-06-01", "status": "executed", "completed_at": "2025-06-01T00:05:00"}]}))
    (tmp_path / "rh_deposits.json").write_text(json.dumps([
        {"date": "2025-01-01", "amount": 1000.0}]))


def test_migrate_then_verify_passes(env):
    config, tmp_path = env
    _seed_json(tmp_path)
    import scripts.migrate_to_sqlite as m
    importlib.reload(m)
    counts = m.migrate()
    assert counts["investors"] == 2
    assert counts["deposits"] == 3
    assert counts["withdrawals"] == 1
    assert counts["pending"] == 1
    assert counts["audit"] == 1
    assert counts["rh_deposits"] == 1
    mismatches = m.verify_migration()
    assert mismatches == [], f"unexpected mismatches: {mismatches}"


def test_backup_copy_created(env):
    config, tmp_path = env
    _seed_json(tmp_path)
    import scripts.migrate_to_sqlite as m
    importlib.reload(m)
    m.migrate()
    backup = tmp_path / "backup_pre_sqlite" / "investors.json"
    assert backup.exists()
    assert json.loads(backup.read_text())["investors"][0]["name"] == "Alice"


def test_verify_detects_corruption(env):
    config, tmp_path = env
    _seed_json(tmp_path)
    import app.db as db
    importlib.reload(db)
    import scripts.migrate_to_sqlite as m
    importlib.reload(m)
    m.migrate()
    # Corrupt the DB: change Alice's deposit amount
    db.get_conn().execute("UPDATE deposits SET amount = amount + 1 WHERE id=1")
    db.get_conn().commit()
    mismatches = m.verify_migration()
    assert mismatches != []
