import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, patch


# ── _format_rh_report ─────────────────────────────────────────────────────────

def test_format_rh_report_no_trades_omits_spy_when_rh_pct_none():
    from app.rh_pnl import _format_rh_report
    msg = _format_rh_report("Monday June 12, 2026", [], 5, 3)
    assert "No trades in this period" in msg
    assert "Portfolio Return" not in msg
    assert "S&P 500" not in msg
    assert "All-Time Record: 5-3" in msg


def test_format_rh_report_includes_portfolio_return_and_spy_comparison():
    from app.rh_pnl import _format_rh_report
    trades = [
        {"ticker": "SPY", "dollar_pnl": 50.0, "is_win": True},
        {"ticker": "SPY", "dollar_pnl": -10.0, "is_win": False},
    ]
    msg = _format_rh_report("Monday June 12, 2026", trades, 10, 4, rh_pct=2.0, spy_pct=1.0)
    assert "Portfolio Return: +2.00%" in msg
    assert "S&P 500: +1.00%" in msg
    assert "OUTPERFORM by 1.00%" in msg


def test_format_rh_report_underperform():
    from app.rh_pnl import _format_rh_report
    trades = [{"ticker": "SPY", "dollar_pnl": -50.0, "is_win": False}]
    msg = _format_rh_report("Monday June 12, 2026", trades, 10, 5, rh_pct=-1.0, spy_pct=1.0)
    assert "Portfolio Return: -1.00%" in msg
    assert "UNDERPERFORM by 2.00%" in msg


def test_format_rh_report_no_trades_still_shows_portfolio_return():
    """Open positions can move the portfolio even with no trades closed this period."""
    from app.rh_pnl import _format_rh_report
    msg = _format_rh_report("Monday June 12, 2026", [], 5, 3, rh_pct=0.75, spy_pct=0.5)
    assert "No trades in this period" in msg
    assert "Portfolio Return: +0.75%" in msg
    assert "OUTPERFORM by 0.25%" in msg


def test_format_rh_report_omits_spy_comparison_when_rh_pct_unavailable():
    """If we couldn't compute RH's own return, don't show a bare SPY line."""
    from app.rh_pnl import _format_rh_report
    trades = [{"ticker": "SPY", "dollar_pnl": 50.0, "is_win": True}]
    msg = _format_rh_report("Monday June 12, 2026", trades, 10, 4, rh_pct=None, spy_pct=1.0)
    assert "S&P 500" not in msg
    assert "Portfolio Return" not in msg


# ── record_rh_equity_snapshot ─────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.rh_pnl.record_snapshot", new_callable=AsyncMock)
@patch("app.rh_pnl.fetch_spy_close_price", return_value=605.0)
@patch("app.rh_pnl.rh_client")
async def test_record_rh_equity_snapshot_records_when_both_available(
    mock_rh_client, mock_fetch_spy, mock_record_snapshot
):
    from app.rh_pnl import record_rh_equity_snapshot

    mock_rh_client.get_portfolio_equity_async = AsyncMock(return_value=10200.0)

    await record_rh_equity_snapshot()

    mock_record_snapshot.assert_called_once()
    date_str, ts, equity, spy_close = mock_record_snapshot.call_args[0]
    assert isinstance(date_str, str)
    assert isinstance(ts, int)
    assert equity == 10200.0
    assert spy_close == 605.0


@pytest.mark.asyncio
@patch("app.rh_pnl.record_snapshot", new_callable=AsyncMock)
@patch("app.rh_pnl.fetch_spy_close_price", return_value=605.0)
@patch("app.rh_pnl.rh_client")
async def test_record_rh_equity_snapshot_skips_when_rh_equity_unavailable(
    mock_rh_client, mock_fetch_spy, mock_record_snapshot
):
    from app.rh_pnl import record_rh_equity_snapshot

    mock_rh_client.get_portfolio_equity_async = AsyncMock(return_value=None)

    await record_rh_equity_snapshot()

    mock_record_snapshot.assert_not_called()


@pytest.mark.asyncio
@patch("app.rh_pnl.record_snapshot", new_callable=AsyncMock)
@patch("app.rh_pnl.fetch_spy_close_price", return_value=None)
@patch("app.rh_pnl.rh_client")
async def test_record_rh_equity_snapshot_skips_when_spy_unavailable(
    mock_rh_client, mock_fetch_spy, mock_record_snapshot
):
    from app.rh_pnl import record_rh_equity_snapshot

    mock_rh_client.get_portfolio_equity_async = AsyncMock(return_value=10200.0)

    await record_rh_equity_snapshot()

    mock_record_snapshot.assert_not_called()


# ── send_rh_report — snapshot-based comparison ────────────────────────────────

@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.notify_rh_pnl_with_chart", new_callable=AsyncMock)
@patch("app.rh_pnl.generate_rh_equity_chart")
@patch("app.rh_pnl.get_snapshots")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_daily_computes_pct_from_last_two_snapshots(
    mock_get_all_trades, mock_get_totals, mock_get_snapshots,
    mock_generate_chart, mock_notify_with_chart, mock_notify,
):
    """Daily report compares the two most recently recorded snapshots —
    RH equity and SPY close are paired, so the % returns can't drift apart."""
    from app.rh_pnl import send_rh_report

    mock_get_snapshots.return_value = [
        {"date": "2026-06-11", "ts": 1749648000, "equity": 10000.0, "spy_close": 600.0},
        {"date": "2026-06-12", "ts": 1749734400, "equity": 10100.0, "spy_close": 603.0},
    ]
    mock_generate_chart.return_value = b"\x89PNGfakechart"

    await send_rh_report("daily")

    mock_generate_chart.assert_called_once()
    call_args = mock_generate_chart.call_args[0]
    assert call_args[0] == [10000.0, 10100.0]
    assert call_args[1] == [1749648000, 1749734400]

    msg = mock_notify_with_chart.call_args[0][0]
    assert "Portfolio Return: +1.00%" in msg
    assert "S&P 500: +0.50%" in msg
    assert "OUTPERFORM by 0.50%" in msg
    mock_notify.assert_not_called()


@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.notify_rh_pnl_with_chart", new_callable=AsyncMock)
@patch("app.rh_pnl.generate_rh_equity_chart")
@patch("app.rh_pnl.get_snapshots")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_daily_no_baseline_when_only_one_snapshot(
    mock_get_all_trades, mock_get_totals, mock_get_snapshots,
    mock_generate_chart, mock_notify_with_chart, mock_notify,
):
    """First day of tracking: only today's snapshot exists, so there's no
    prior day to diff against — report still sends without Portfolio Return."""
    from app.rh_pnl import send_rh_report

    mock_get_snapshots.return_value = [
        {"date": "2026-06-12", "ts": 1749734400, "equity": 10100.0, "spy_close": 603.0},
    ]

    await send_rh_report("daily")

    mock_generate_chart.assert_not_called()
    msg = mock_notify.call_args[0][0]
    assert "Portfolio Return" not in msg
    assert "S&P 500" not in msg
    mock_notify_with_chart.assert_not_called()


@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.notify_rh_pnl_with_chart", new_callable=AsyncMock)
@patch("app.rh_pnl.get_snapshots", return_value=[])
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_no_snapshots_omits_portfolio_return(
    mock_get_all_trades, mock_get_totals, mock_get_snapshots, mock_notify_with_chart, mock_notify,
):
    """No snapshots recorded yet at all — report still sends, just without
    the Portfolio Return / S&P 500 comparison lines."""
    from app.rh_pnl import send_rh_report

    await send_rh_report("weekly")

    mock_notify_with_chart.assert_not_called()
    msg = mock_notify.call_args[0][0]
    assert "Portfolio Return" not in msg
    assert "S&P 500" not in msg
    assert "No trades in this period" in msg


@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.notify_rh_pnl_with_chart", new_callable=AsyncMock)
@patch("app.rh_pnl.generate_rh_equity_chart")
@patch("app.rh_pnl._period_start_date", return_value="2026-06-08")
@patch("app.rh_pnl.get_snapshots")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_weekly_uses_period_start_date_baseline(
    mock_get_all_trades, mock_get_totals, mock_get_snapshots, mock_period_start_date,
    mock_generate_chart, mock_notify_with_chart, mock_notify,
):
    """Weekly report's baseline is the first snapshot on/after the start of
    the week — earlier snapshots are excluded from both the % return and chart."""
    from app.rh_pnl import send_rh_report

    mock_get_snapshots.return_value = [
        {"date": "2026-06-05", "ts": 1749100000, "equity": 9800.0, "spy_close": 590.0},   # last week — excluded
        {"date": "2026-06-08", "ts": 1749400000, "equity": 10000.0, "spy_close": 600.0},  # Monday — baseline
        {"date": "2026-06-12", "ts": 1749734400, "equity": 10200.0, "spy_close": 606.0},  # today — latest
    ]
    mock_generate_chart.return_value = b"\x89PNGfakechart"

    await send_rh_report("weekly")

    chart_args = mock_generate_chart.call_args[0]
    assert chart_args[0] == [10000.0, 10200.0]
    assert chart_args[1] == [1749400000, 1749734400]

    msg = mock_notify_with_chart.call_args[0][0]
    assert "Portfolio Return: +2.00%" in msg
    assert "S&P 500: +1.00%" in msg


@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.notify_rh_pnl_with_chart", new_callable=AsyncMock)
@patch("app.rh_pnl.generate_rh_pnl_chart")
@patch("app.rh_pnl.generate_rh_equity_chart")
@patch("app.rh_pnl._period_start_date", return_value="2026-06-08")
@patch("app.rh_pnl.get_snapshots")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades")
async def test_send_rh_report_falls_back_to_pnl_chart_when_baseline_equals_latest(
    mock_get_all_trades, mock_get_totals, mock_get_snapshots, mock_period_start_date,
    mock_generate_equity_chart, mock_generate_pnl_chart, mock_notify_with_chart, mock_notify,
):
    """Only one snapshot recorded so far this period (it's both baseline and
    latest): 0% return so far, nothing to plot on the equity chart — fall back
    to the trade-based $ P&L chart when there are enough trades."""
    from app.rh_pnl import send_rh_report

    now = datetime.now(timezone.utc)
    mock_get_all_trades.return_value = [
        {"ts": now.isoformat(), "dollar_pnl": 50.0, "is_win": True, "ticker": "SPY"},
        {"ts": now.isoformat(), "dollar_pnl": -10.0, "is_win": False, "ticker": "SPY"},
    ]
    mock_get_snapshots.return_value = [
        {"date": "2026-06-12", "ts": 1749734400, "equity": 10100.0, "spy_close": 603.0},
    ]
    mock_generate_pnl_chart.return_value = b"\x89PNGfallback"

    await send_rh_report("weekly")

    mock_generate_equity_chart.assert_not_called()
    mock_generate_pnl_chart.assert_called_once()
    mock_notify_with_chart.assert_called_once()
    assert mock_notify_with_chart.call_args[0][1] == b"\x89PNGfallback"
    msg = mock_notify_with_chart.call_args[0][0]
    assert "Portfolio Return: +0.00%" in msg
    assert "S&P 500: +0.00%" in msg
