"""Deposit sourcing for the Alpaca fund P&L adjustment.

Regression coverage for the July-17 bug: an investor deposit inflated the P&L
chart because the deposit adjustment was fed only by the laggy Alpaca
account-activities API. Every deposit goes through /deposit, so the investor
ledger is the authoritative, immediately-updated source of truth.
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
    """Epoch seconds for a 4 PM ET market-close snapshot on the given date."""
    return int(_ET.localize(datetime(y, m, d, 16, 0)).timestamp())


def test_deposit_events_prefers_investor_ledger_over_api():
    from app import pnl
    with patch("app.investors.get_deposit_events", return_value=[("2026-07-17", 842.68)]), \
         patch("app.pnl.get_alpaca_deposit_events", return_value=[("2026-01-01", 5000.0)]):
        assert pnl._deposit_events() == [("2026-07-17", 842.68)]


def test_deposit_events_falls_back_to_api_when_ledger_empty():
    from app import pnl
    with patch("app.investors.get_deposit_events", return_value=[]), \
         patch("app.pnl.get_alpaca_deposit_events", return_value=[("2026-01-01", 5000.0)]):
        assert pnl._deposit_events() == [("2026-01-01", 5000.0)]


def test_july17_investor_deposit_does_not_spike_the_chart():
    """The reported bug: a $842.68 deposit on 2026-07-17 (well under the 20%
    auto-detect threshold, and not yet in the Alpaca API) must not appear as a
    gain once the ledger is consulted — the adjusted equity is flat across it."""
    from app import pnl
    # Flat $10,000, then +$842.68 on Jul 17 purely from the deposit (no trading).
    equity     = [10000.0, 10000.0, 10842.68, 10842.68]
    timestamps = [_ts(2026, 7, 15), _ts(2026, 7, 16), _ts(2026, 7, 17), _ts(2026, 7, 18)]

    with patch("app.investors.get_deposit_events", return_value=[("2026-07-17", 842.68)]), \
         patch("app.pnl.get_alpaca_deposit_events", return_value=[]):
        events = pnl._deposit_events()
        adjusted = pnl.deposit_adjusted_equity(equity, timestamps, events)

    # Deposit stripped out → no spike; the series is flat (0 trading P&L).
    assert adjusted == [10000.0, 10000.0, 10000.0, 10000.0]
    assert max(adjusted) - min(adjusted) == 0.0


def test_without_ledger_the_subthreshold_deposit_would_still_spike():
    """Documents *why* the API-only path failed: an 8.4% deposit jump is below
    the 20% auto-detect threshold, so with no explicit event it is NOT stripped
    — proving the ledger source is what fixes it."""
    from app import pnl
    equity     = [10000.0, 10000.0, 10842.68, 10842.68]
    timestamps = [_ts(2026, 7, 15), _ts(2026, 7, 16), _ts(2026, 7, 17), _ts(2026, 7, 18)]

    # No events from any source → auto-detect only, which misses a sub-20% jump.
    adjusted = pnl.deposit_adjusted_equity(equity, timestamps, [])
    assert adjusted[-1] == 10842.68  # the spike survives — the old behaviour
