import importlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    monkeypatch.setenv("PENDING_WITHDRAWALS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setenv("WITHDRAWAL_AUDIT_PATH", str(tmp_path / "audit.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.investors as investors
    importlib.reload(investors)
    import app.pending_withdrawals as pw
    importlib.reload(pw)
    import app.withdrawal_audit as wa
    importlib.reload(wa)
    yield db, investors, pw, wa
    db.reset_for_tests()


def test_crash_between_ledger_and_audit_rolls_back(env):
    db, investors, pw, wa = env
    # Seed a ledger with one investor holding units
    investors.save_investors([investors.Investor(name="Alice",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-01")])])
    pw.save_pending_withdrawal("wd-1", "Alice", 100.0, "2025-06-01", "2025-06-02")

    before = investors.serialize_investors(investors.load_investors())

    # Simulate the atomic block failing after the ledger write but before audit
    with pytest.raises(RuntimeError):
        with db.transaction():
            invs = investors.load_investors()
            invs[0].withdrawals.append(investors.Withdrawal(
                units=0.2, exit_spy=540.0, cost_basis=100.0, proceeds=108.0, date="2025-06-02"))
            investors.save_investors(invs)
            pw.remove_pending_withdrawal("wd-1")
            raise RuntimeError("crash before audit write")

    # Everything rolled back: ledger unchanged, pending still present, no audit entry
    assert investors.serialize_investors(investors.load_investors()) == before
    assert pw.get_pending_withdrawal("wd-1") is not None
    assert wa.load_withdrawal_audit() == []


def test_successful_transaction_commits_all_three(env):
    db, investors, pw, wa = env
    investors.save_investors([investors.Investor(name="Bob",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-01")])])
    pw.save_pending_withdrawal("wd-2", "Bob", 100.0, "2025-06-01", "2025-06-02")

    with db.transaction():
        invs = investors.load_investors()
        invs[0].withdrawals.append(investors.Withdrawal(
            units=0.2, exit_spy=540.0, cost_basis=100.0, proceeds=108.0, date="2025-06-02"))
        investors.save_investors(invs)
        pw.remove_pending_withdrawal("wd-2")
        wa.append_withdrawal_audit("wd-2", "Bob", 100.0, "2025-06-01", "2025-06-02",
                                   "executed", completed_at="2025-06-02T00:05:00")

    assert len(investors.load_investors()[0].withdrawals) == 1
    assert pw.get_pending_withdrawal("wd-2") is None
    assert wa.load_withdrawal_audit()[0]["status"] == "executed"
