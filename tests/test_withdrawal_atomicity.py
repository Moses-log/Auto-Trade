import importlib
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    monkeypatch.setenv("PENDING_WITHDRAWALS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setenv("WITHDRAWAL_AUDIT_PATH", str(tmp_path / "audit.json"))
    import app.config as config
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


def _mock_account(equity: float):
    account = MagicMock()
    account.equity = str(equity)
    return account


async def _none():
    return None


def _reload_withdrawal_execution():
    """execute_pending_withdrawal binds load_investors/save_investors/etc. at
    import time via `from app.investors import ...`; those names must be
    re-resolved against the freshly-reloaded db-backed modules from `env`
    before we drive the real function end to end."""
    import app.withdrawal_execution as we
    importlib.reload(we)
    return we


@pytest.mark.asyncio
async def test_execute_pending_withdrawal_e2e_sqlite_failure_rolls_back(env):
    """Drives the real execute_pending_withdrawal (not the transaction()
    primitive) under use_sqlite=True and forces a mid-transaction failure by
    making the "executed" append_withdrawal_audit call raise. Confirms the
    post-lock guards route correctly: no double pending-removal, no ledger
    write survives, and the failure is recorded via the post-lock autocommit
    'failed' audit call."""
    db, investors, pw, wa = env
    we = _reload_withdrawal_execution()

    investors.save_investors([investors.Investor(name="Carol",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-01")])])
    pw.save_pending_withdrawal("wd-e2e-fail", "Carol", 100.0,
                                "2025-06-01T00:00:00", "2025-06-02T00:00:00")

    before = investors.serialize_investors(investors.load_investors())

    real_append = we.append_withdrawal_audit
    call_count = {"n": 0}

    def fake_append(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulates a crash after the ledger write but before the audit
            # commit inside `with db.transaction():` — the whole batch,
            # including save_investors and remove_pending_withdrawal, must
            # roll back.
            raise RuntimeError("simulated mid-transaction crash")
        return real_append(*args, **kwargs)

    with patch("app.withdrawal_execution.get_account", return_value=_mock_account(2000.0)), \
         patch("app.withdrawal_execution.get_latest_price", return_value=540.0), \
         patch("app.withdrawal_execution.append_withdrawal_audit", side_effect=fake_append), \
         patch("app.withdrawal_execution.notify_investors") as mock_notify:
        mock_notify.return_value = _none()
        await we.execute_pending_withdrawal("wd-e2e-fail")

    # Ledger unchanged — the transaction rolled back the save_investors write.
    assert investors.serialize_investors(investors.load_investors()) == before
    # Pending withdrawal still present — not removed.
    assert pw.get_pending_withdrawal("wd-e2e-fail") is not None
    # Exactly one audit entry, written via the post-lock autocommit path, status "failed".
    audit = wa.load_withdrawal_audit()
    assert len(audit) == 1
    assert audit[0]["status"] == "failed"
    # No success notification for the executed withdrawal was sent — only the failure notice.
    mock_notify.assert_called_once()
    sent_msg = mock_notify.call_args[0][0]
    assert "failed" in sent_msg.lower()


@pytest.mark.asyncio
async def test_execute_pending_withdrawal_e2e_sqlite_success_commits_all_three(env):
    """Same setup, no injected failure — proves the guards route the other
    way through the real function too (no double-write, no missing write)."""
    db, investors, pw, wa = env
    we = _reload_withdrawal_execution()

    investors.save_investors([investors.Investor(name="Dave",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-01")])])
    pw.save_pending_withdrawal("wd-e2e-ok", "Dave", 100.0,
                                "2025-06-01T00:00:00", "2025-06-02T00:00:00")

    with patch("app.withdrawal_execution.get_account", return_value=_mock_account(2000.0)), \
         patch("app.withdrawal_execution.get_latest_price", return_value=540.0), \
         patch("app.withdrawal_execution.notify_investors") as mock_notify, \
         patch("app.withdrawal_execution.push_backup") as mock_backup:
        mock_notify.return_value = _none()
        mock_backup.return_value = _none()
        await we.execute_pending_withdrawal("wd-e2e-ok")

    updated = investors.load_investors()
    assert len(updated[0].withdrawals) == 1
    assert pw.get_pending_withdrawal("wd-e2e-ok") is None
    audit = wa.load_withdrawal_audit()
    assert len(audit) == 1
    assert audit[0]["status"] == "executed"
    mock_notify.assert_called_once()
