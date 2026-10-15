"""Withdrawals must not count as trading losses in the Alpaca fund P&L.

Oct-14-2026 incident: a $4,163.02 investor cash-out was reported as
"P&L: -$4,205.66 (-43.50%)" because only deposits were stripped. The real
move was -$42.64, in line with SPY.
"""
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytz

_ET = pytz.timezone("America/New_York")


def _ts(y, m, d):
    return int(_ET.localize(datetime(y, m, d, 16, 0)).timestamp())


def test_withdrawal_aligns_to_negative_jump():
    from app.pnl import align_deposits_to_bars
    equity = [9667.24, 5461.58]
    bar_dates = [date(2026, 10, 13), date(2026, 10, 14)]
    by_index = align_deposits_to_bars(equity, bar_dates, [("2026-10-13", -4163.02)])
    assert dict(by_index) == {1: -4163.02}


def test_withdrawal_ignored_without_a_matching_drop():
    from app.pnl import align_deposits_to_bars
    # Equity rose — no cash left in-window, nothing to strip.
    by_index = align_deposits_to_bars([9667.24, 9700.0],
                                      [date(2026, 10, 13), date(2026, 10, 14)],
                                      [("2026-10-13", -4163.02)])
    assert dict(by_index) == {}


def test_daily_pnl_excludes_withdrawal():
    from app import pnl
    history = SimpleNamespace(
        equity=[9667.24, 5461.58],
        timestamp=[_ts(2026, 10, 13), _ts(2026, 10, 14)],
    )
    with patch("app.pnl._deposit_events", return_value=[]), \
         patch("app.pnl.get_withdrawal_events", return_value=[("2026-10-13", -4163.02)]):
        result = pnl._compute_pnl(history, "daily")
    assert result.close_equity == pytest.approx(5461.58)
    assert result.dollar_pnl == pytest.approx(-42.64, abs=0.01)
    assert result.pct_pnl == pytest.approx(-42.64 / 9667.24 * 100, abs=0.01)


def test_cash_flow_events_merge_deposits_and_withdrawals():
    from app import pnl
    with patch("app.pnl._deposit_events", return_value=[("2026-07-17", 842.68)]), \
         patch("app.pnl.get_withdrawal_events", return_value=[("2026-10-13", -4163.02)]):
        assert pnl._cash_flow_events() == [("2026-07-17", 842.68), ("2026-10-13", -4163.02)]


def test_cash_flow_events_survive_ledger_error():
    from app import pnl
    with patch("app.pnl._deposit_events", return_value=[("2026-07-17", 842.68)]), \
         patch("app.pnl.get_withdrawal_events", side_effect=ValueError("malformed")):
        assert pnl._cash_flow_events() == [("2026-07-17", 842.68)]


def test_get_withdrawal_events_from_ledger():
    from app import investors
    ledger = [investors.Investor(name="David",
        deposits=[investors.Deposit(amount=4000.0, entry_spy=700.0, date="2026-05-01")],
        withdrawals=[investors.Withdrawal(units=5.7, exit_spy=777.66, cost_basis=4000.0,
                                          proceeds=4163.02, date="2026-10-13")])]
    with patch("app.investors.load_investors", return_value=ledger):
        assert investors.get_withdrawal_events() == [("2026-10-13", -4163.02)]
