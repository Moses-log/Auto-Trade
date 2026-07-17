"""Deposit sourcing + adjustment for the Alpaca fund P&L.

Covers the July-2026 chart incidents: deposits inflating the curve, transient
spikes from date-lag, and the -88% crater from stripping baseline capital.
Deposits are sourced from Alpaca ORDERS (dollar/notional buys) and only
in-window equity jumps are stripped; a guardrail prevents any crater.
"""
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import datetime
from unittest.mock import patch

import pytz

_ET = pytz.timezone("America/New_York")


def _ts(y, m, d):
    return int(_ET.localize(datetime(y, m, d, 16, 0)).timestamp())


# ── source selection ──────────────────────────────────────────────────────────

def test_deposit_events_prefers_orders_over_activities():
    from app import pnl
    with patch("app.pnl.get_deposit_events_from_orders", return_value=[("2026-07-17", 842.68)]), \
         patch("app.pnl.get_alpaca_deposit_events", return_value=[("2026-01-01", 5000.0)]):
        assert pnl._deposit_events() == [("2026-07-17", 842.68)]


def test_deposit_events_falls_back_to_activities_when_no_orders():
    from app import pnl
    with patch("app.pnl.get_deposit_events_from_orders", return_value=[]), \
         patch("app.pnl.get_alpaca_deposit_events", return_value=[("2026-01-01", 5000.0)]):
        assert pnl._deposit_events() == [("2026-01-01", 5000.0)]


# ── the adjustment ────────────────────────────────────────────────────────────

def test_in_window_deposit_is_stripped_at_its_jump():
    from app import pnl
    equity     = [10000.0, 10000.0, 10842.68, 10842.68]   # +842.68 lands at index 2
    timestamps = [_ts(2026, 7, 14), _ts(2026, 7, 15), _ts(2026, 7, 16), _ts(2026, 7, 17)]
    adj = pnl.deposit_adjusted_equity(equity, timestamps, [("2026-07-16", 842.68)])
    assert adj == [10000.0, 10000.0, 10000.0, 10000.0]


def test_late_recorded_deposit_aligns_to_the_jump_no_spike():
    """Deposit recorded a bar after the cash appears — still aligned, no spike."""
    from app import pnl
    equity     = [10000.0, 10000.0, 10842.68, 10842.68, 10842.68]  # jump at index 2 (Jul 16)
    timestamps = [_ts(2026, 7, 14), _ts(2026, 7, 15), _ts(2026, 7, 16),
                  _ts(2026, 7, 17), _ts(2026, 7, 18)]
    adj = pnl.deposit_adjusted_equity(equity, timestamps, [("2026-07-17", 842.68)])  # recorded late
    assert adj == [10000.0] * 5


def test_baseline_capital_before_window_is_never_stripped():
    """The -88% crater guard: a founding deposit that predates the window leaves
    no in-window jump and must NOT be stripped, even if its recorded date sits a
    few days before the first bar."""
    from app import pnl
    # Clean trading week; the $3000 founding deposit was invested days before it.
    equity     = [5300.0, 5320.0, 5310.0, 5335.0]
    timestamps = [_ts(2026, 4, 27), _ts(2026, 4, 28), _ts(2026, 4, 29), _ts(2026, 4, 30)]
    adj = pnl.deposit_adjusted_equity(equity, timestamps, [("2026-04-24", 3000.0)])
    assert adj == equity  # untouched — no in-window jump matches it


def test_masked_deposit_with_no_matching_jump_is_skipped():
    """If no in-window jump plausibly matches the deposit amount, skip it rather
    than blindly subtracting (which was the over-strip failure mode)."""
    from app import pnl
    equity     = [10000.0, 10010.0, 10025.0, 10040.0]   # only small trading moves
    timestamps = [_ts(2026, 7, 14), _ts(2026, 7, 15), _ts(2026, 7, 16), _ts(2026, 7, 17)]
    adj = pnl.deposit_adjusted_equity(equity, timestamps, [("2026-07-15", 5000.0)])
    assert adj == equity  # no ~5000 jump → not stripped


def test_two_deposits_only_the_two_are_stripped():
    from app import pnl
    equity = [5000.0, 5000.0, 6500.0, 6500.0, 6500.0, 7342.68, 7342.68]
    timestamps = [_ts(2026, 4, 27), _ts(2026, 6, 17), _ts(2026, 6, 18), _ts(2026, 6, 19),
                  _ts(2026, 7, 16), _ts(2026, 7, 17), _ts(2026, 7, 18)]
    events = [("2026-06-18", 1500.0), ("2026-07-17", 842.68)]
    adj = pnl.deposit_adjusted_equity(equity, timestamps, events)
    assert adj == [5000.0] * 7                      # both stripped, flat
    assert min(adj) == 5000.0                        # no crater


def test_guardrail_returns_raw_when_adjustment_goes_negative():
    """Belt-and-suspenders: if the alignment ever over-subtracts into negative
    equity (a bug), abandon the adjustment and return raw equity — never a
    catastrophic crater."""
    from app import pnl
    equity     = [5000.0, 5100.0, 5100.0]
    timestamps = [_ts(2026, 7, 15), _ts(2026, 7, 16), _ts(2026, 7, 17)]
    with patch("app.pnl._align_deposits_to_equity", return_value={1: 999999.0}):
        adj = pnl.deposit_adjusted_equity(equity, timestamps, [("2026-07-16", 999999.0)])
    assert adj == equity  # guardrail tripped → raw returned


def test_no_events_falls_back_to_autodetect_large_jump():
    from app import pnl
    equity     = [1000.0, 1000.0, 5000.0, 5000.0]   # +400% jump = obvious deposit
    timestamps = [_ts(2026, 7, 14), _ts(2026, 7, 15), _ts(2026, 7, 16), _ts(2026, 7, 17)]
    adj = pnl.deposit_adjusted_equity(equity, timestamps, [])
    assert adj == [1000.0, 1000.0, 1000.0, 1000.0]
