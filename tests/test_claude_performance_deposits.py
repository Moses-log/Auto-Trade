"""Regression tests for deposit stripping in get_claude_performance.

Guards the bug where a deposit dated on a non-trading day (weekend/holiday)
matched no equity snapshot's date and was therefore never subtracted, showing
up as a phantom portfolio gain on the Portfolio Manager track-record chart.
"""
import os
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from app.claude_callouts import get_claude_performance


def _snaps(*rows):
    # rows: (date, equity) with a flat SPY so portfolio_pct is isolated
    return [
        {"date": d, "ts": i, "equity": eq, "spy_close": 600.0}
        for i, (d, eq) in enumerate(rows, start=1)
    ]


def _run(snapshots, deposits):
    with patch("app.rh_equity_history.get_snapshots", return_value=snapshots), \
         patch("app.rh_deposit_log.get_rh_deposit_events", return_value=deposits):
        return get_claude_performance()


def test_weekend_dated_deposit_is_stripped_not_counted_as_gain():
    # $1000 deposit dated Sunday 2026-07-12 lands in Mon 2026-07-13 equity.
    snaps = _snaps(("2026-07-08", 1000.0), ("2026-07-09", 1010.0), ("2026-07-13", 2010.0))
    result = _run(snaps, [("2026-07-12", 1000.0)])
    # Real return is ~1% (1000 -> 1010), not ~101% from the deposit.
    assert result["portfolio_pct"] == 1.0


def test_trading_day_deposit_takes_effect_next_snapshot():
    # Deposit dated on a trading day is "pending" that day (T+1), so it is not
    # subtracted from that day's snapshot but is from the following one.
    snaps = _snaps(("2026-07-08", 1000.0), ("2026-07-09", 1000.0), ("2026-07-10", 2000.0))
    result = _run(snaps, [("2026-07-09", 1000.0)])
    assert result["portfolio_pct"] == 0.0


def test_same_day_deposit_leaves_no_transient_spike():
    # July 24 incident: the $100 landed in the SAME day's equity snapshot, but the
    # deposit was only subtracted from the NEXT snapshot onward, leaving a one-bar
    # phantom spike that self-corrected the following trading day.
    snaps = _snaps(("2026-07-23", 1000.0), ("2026-07-24", 2000.0), ("2026-07-27", 2000.0))
    result = _run(snaps, [("2026-07-24", 1000.0)])
    pcts = [p["portfolio_pct"] for p in result["data_points"]]
    assert pcts == [0.0, 0.0, 0.0], f"transient deposit spike in series: {pcts}"


def test_baseline_capital_is_never_stripped():
    # A deposit dated at/before the first snapshot is already inside the baseline
    # equity. Stripping it again is what cratered the chart previously.
    snaps = _snaps(("2026-07-08", 1000.0), ("2026-07-09", 1100.0))
    result = _run(snaps, [("2026-07-08", 900.0)])
    assert result["portfolio_pct"] == 10.0


def test_no_deposits_leaves_returns_untouched():
    snaps = _snaps(("2026-07-08", 1000.0), ("2026-07-09", 1100.0))
    result = _run(snaps, [])
    assert result["portfolio_pct"] == 10.0
