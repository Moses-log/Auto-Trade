"""Locked withdrawals + failed-withdrawal cleanup.

Covers the Oct-2026 David incident: a full withdrawal scheduled with
spy_price (the investor's SPY was sold to cash at that price) failed 24h later
with "exceeds available equity" because execution re-priced his units off the
pooled NAV, which had since absorbed the other investors' SPY loss. A locked
withdrawal must freeze the units redeemed at schedule time.
"""
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
    import app.withdrawal_execution as we
    importlib.reload(we)
    yield db, investors, pw, wa, we
    db.reset_for_tests()


def _mock_account(equity: float):
    account = MagicMock()
    account.equity = str(equity)
    return account


async def _none():
    return None


def _seed_two_investors(investors):
    # David and Other each hold 5 units bought at $700 → $3,500 each.
    investors.save_investors([
        investors.Investor(name="David",
            deposits=[investors.Deposit(amount=3500.0, entry_spy=700.0, date="2026-05-01")]),
        investors.Investor(name="Other",
            deposits=[investors.Deposit(amount=3500.0, entry_spy=700.0, date="2026-05-01")]),
    ])


# ── compute_withdrawal_lots with frozen units ─────────────────────────────────

def test_lots_with_explicit_units_pay_exact_amount(env):
    _, investors, *_ = env
    inv = investors.Investor(name="A",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2026-01-01")])
    # NAV argument is deliberately stale/low — explicit units must override it.
    lots, units = investors.compute_withdrawal_lots(inv, 1100.0, 1.0, units=2.0)
    assert units == pytest.approx(2.0)
    assert sum(l["proceeds"] for l in lots) == pytest.approx(1100.0)
    assert sum(l["units"] for l in lots) == pytest.approx(2.0)


def test_lots_with_explicit_units_reject_more_than_held(env):
    _, investors, *_ = env
    inv = investors.Investor(name="A",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2026-01-01")])
    with pytest.raises(ValueError, match="exceeds"):
        investors.compute_withdrawal_lots(inv, 1100.0, 550.0, units=2.5)


def test_full_unlocked_withdrawal_redeems_exactly_all_units(env):
    _, investors, *_ = env
    inv = investors.Investor(name="A",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=300.0, date="2026-01-01")])
    nav = 333.333333
    available = investors._net_units(inv) * nav
    _, units = investors.compute_withdrawal_lots(inv, round(available, 2), nav)
    assert units == investors._net_units(inv)


# ── pending store carries locked_units ────────────────────────────────────────

def test_pending_store_round_trips_locked_units(env):
    _, _, pw, *_ = env
    pw.save_pending_withdrawal("wd-l", "David", 100.0, "t0", "t1",
                               spy_price=777.66, locked_units=0.125)
    rec = pw.get_pending_withdrawal("wd-l")
    assert rec["locked_units"] == pytest.approx(0.125)
    assert rec["spy_price"] == pytest.approx(777.66)


def test_init_schema_adds_locked_units_to_existing_table(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE pending_withdrawals (id TEXT PRIMARY KEY, investor TEXT NOT NULL, "
                "amount REAL NOT NULL, requested_at TEXT NOT NULL, run_at TEXT NOT NULL, spy_price REAL)")
    old.commit()
    old.close()
    monkeypatch.setenv("KIMI_DB_PATH", str(path))
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    db.init_schema()  # idempotent
    cols = [r["name"] for r in db.get_conn().execute("PRAGMA table_info(pending_withdrawals)")]
    assert "locked_units" in cols
    db.reset_for_tests()


# ── end to end: the David scenario ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_locked_full_withdrawal_survives_pool_drop(env):
    _, investors, pw, wa, we = env
    _seed_two_investors(investors)

    # Schedule: equity $7,000 → NAV $700/unit; David's 5 units = $3,500, sold to cash.
    with patch("app.withdrawal_execution.get_account", return_value=_mock_account(7000.0)), \
         patch("app.withdrawal_execution.scheduler") as sched:
        rec = await we.schedule_withdrawal("David", 3500.0, spy_price=700.0)
        sched.add_job.assert_called_once()
    pending = pw.get_pending_withdrawal(rec["id"])
    assert pending["locked_units"] == pytest.approx(5.0)

    # 24h later Other's SPY fell 10%: equity = 3,500 cash + 3,150 SPY = 6,650.
    # Pooled NAV would value David at 3,325 — the old code failed here.
    with patch("app.withdrawal_execution.get_account", return_value=_mock_account(6650.0)), \
         patch("app.withdrawal_execution.get_latest_price", return_value=630.0), \
         patch("app.withdrawal_execution.push_backup", return_value=_none()), \
         patch("app.withdrawal_execution.notify_investors") as notify:
        notify.return_value = _none()
        await we.execute_pending_withdrawal(rec["id"])

    david = next(i for i in investors.load_investors() if i.name == "David")
    assert len(david.withdrawals) == 1
    w = david.withdrawals[0]
    assert w.units == pytest.approx(5.0)
    assert w.proceeds == pytest.approx(3500.0)
    assert w.exit_spy == pytest.approx(700.0)
    assert investors._net_units(david) == pytest.approx(0.0, abs=1e-9)
    assert pw.get_pending_withdrawal(rec["id"]) is None
    assert wa.load_withdrawal_audit()[-1]["status"] == "executed"
    # Other now owns the whole remaining pool.
    remaining = investors.compute_breakdown(investors.load_investors(), 630.0, 6650.0 - 3500.0)
    other = next(r for r in remaining.investors if r.name == "Other")
    assert other.current_equity == pytest.approx(3150.0)


@pytest.mark.asyncio
async def test_failed_withdrawal_is_removed_from_pending_in_sqlite_mode(env):
    _, investors, pw, wa, we = env
    _seed_two_investors(investors)
    pw.save_pending_withdrawal("wd-big", "David", 99999.0, "2026-10-13T11:35:56", "2026-10-14T11:35:56")

    with patch("app.withdrawal_execution.get_account", return_value=_mock_account(7000.0)), \
         patch("app.withdrawal_execution.get_latest_price", return_value=700.0), \
         patch("app.withdrawal_execution.notify_investors") as notify:
        notify.return_value = _none()
        await we.execute_pending_withdrawal("wd-big")

    # Otherwise reschedule_pending_withdrawals re-fires it on every restart.
    assert pw.get_pending_withdrawal("wd-big") is None
    assert wa.load_withdrawal_audit()[-1]["status"] == "failed"
