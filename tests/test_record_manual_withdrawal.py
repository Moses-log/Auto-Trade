"""scripts/record_manual_withdrawal.py — ledger repair for a withdrawal whose
cash already left Alpaca but whose scheduled ledger write failed."""
import importlib
from unittest.mock import patch

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
    import scripts.record_manual_withdrawal as rmw
    importlib.reload(rmw)
    investors.save_investors([
        investors.Investor(name="David", deposits=[
            investors.Deposit(amount=2000.0, entry_spy=700.0, date="2026-05-01"),
            investors.Deposit(amount=2000.0, entry_spy=740.0, date="2026-08-01"),
        ]),
        investors.Investor(name="Other", deposits=[
            investors.Deposit(amount=5000.0, entry_spy=700.0, date="2026-05-01"),
        ]),
    ])
    pw.save_pending_withdrawal("wd-stale", "David", 4163.02,
                               "2026-10-13T11:35:56", "2026-10-14T11:35:56", spy_price=777.66)
    yield db, investors, pw, wa, rmw
    db.reset_for_tests()


_ARGS = ["--investor", "david", "--amount", "4163.02", "--exit-spy", "777.66",
         "--date", "2026-10-13", "--all-units", "--no-backup"]


def test_dry_run_changes_nothing(env, capsys):
    _, investors, pw, wa, rmw = env
    before = investors.serialize_investors(investors.load_investors())
    assert rmw.main(_ARGS) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "wd-stale" in out
    assert investors.serialize_investors(investors.load_investors()) == before
    assert pw.get_pending_withdrawal("wd-stale") is not None
    assert wa.load_withdrawal_audit() == []


def test_apply_records_full_redemption_and_clears_pending(env, capsys):
    _, investors, pw, wa, rmw = env
    assert rmw.main(_ARGS + ["--apply"]) == 0
    david = next(i for i in investors.load_investors() if i.name == "David")
    assert len(david.withdrawals) == 1
    w = david.withdrawals[0]
    assert w.proceeds == pytest.approx(4163.02)
    assert w.exit_spy == pytest.approx(777.66)
    assert w.date == "2026-10-13"
    assert w.cost_basis == pytest.approx(4000.0)  # both FIFO lots fully consumed
    assert investors._net_units(david) == pytest.approx(0.0, abs=1e-9)
    assert pw.get_pending_withdrawal("wd-stale") is None
    audit = wa.load_withdrawal_audit()
    assert audit[-1]["status"] == "executed_manual"
    assert audit[-1]["investor"] == "David"
    # Other untouched.
    other = next(i for i in investors.load_investors() if i.name == "Other")
    assert other.withdrawals == []


def test_apply_refuses_when_investor_has_no_units(env):
    _, investors, pw, wa, rmw = env
    assert rmw.main(_ARGS + ["--apply"]) == 0
    # Second run must not double-record.
    assert rmw.main(_ARGS + ["--apply"]) == 1
    david = next(i for i in investors.load_investors() if i.name == "David")
    assert len(david.withdrawals) == 1


def test_unknown_investor_exits_nonzero(env):
    *_, rmw = env
    assert rmw.main(["--investor", "Nobody", "--amount", "1", "--exit-spy", "1",
                     "--date", "2026-10-13", "--all-units", "--no-backup"]) == 1
