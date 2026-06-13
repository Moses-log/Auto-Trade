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


# ── send_rh_report wiring ─────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.compute_spy_pct")
@patch("app.rh_pnl.rh_client")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_daily_uses_day_span_and_1d_spy(
    mock_get_all_trades, mock_get_totals, mock_rh_client, mock_compute_spy_pct, mock_notify
):
    from app.rh_pnl import send_rh_report

    mock_rh_client.get_portfolio_pct_change_async = AsyncMock(return_value=0.5)
    mock_rh_client.get_equity_history_async = AsyncMock(return_value=None)
    mock_compute_spy_pct.return_value = 0.3

    await send_rh_report("daily")

    mock_rh_client.get_portfolio_pct_change_async.assert_called_once()
    call_args = mock_rh_client.get_portfolio_pct_change_async.call_args
    assert call_args[0][0] == "day"
    assert call_args[0][1] == "5minute"
    mock_compute_spy_pct.assert_called_once_with("1d")

    msg = mock_notify.call_args[0][0]
    assert "Portfolio Return: +0.50%" in msg
    assert "S&P 500: +0.30%" in msg


@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.compute_spy_pct")
@patch("app.rh_pnl.rh_client")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_ytd_passes_since_filter(
    mock_get_all_trades, mock_get_totals, mock_rh_client, mock_compute_spy_pct, mock_notify
):
    from app.rh_pnl import send_rh_report

    mock_rh_client.get_portfolio_pct_change_async = AsyncMock(return_value=5.0)
    mock_rh_client.get_equity_history_async = AsyncMock(return_value=None)
    mock_compute_spy_pct.return_value = 4.0

    await send_rh_report("ytd")

    call_args = mock_rh_client.get_portfolio_pct_change_async.call_args
    assert call_args[0][0] == "year"
    assert call_args[0][1] == "day"
    assert call_args[1]["since"] is not None
    assert call_args[1]["since"].month == 1 and call_args[1]["since"].day == 1
    mock_compute_spy_pct.assert_called_once_with("ytd")


@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.compute_spy_pct", return_value=None)
@patch("app.rh_pnl.rh_client")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_handles_missing_spy_and_rh_pct(
    mock_get_all_trades, mock_get_totals, mock_rh_client, mock_compute_spy_pct, mock_notify
):
    """If RH/SPY data are unavailable, the report still sends without those lines."""
    from app.rh_pnl import send_rh_report

    mock_rh_client.get_portfolio_pct_change_async = AsyncMock(return_value=None)
    mock_rh_client.get_equity_history_async = AsyncMock(return_value=None)

    await send_rh_report("weekly")

    msg = mock_notify.call_args[0][0]
    assert "Portfolio Return" not in msg
    assert "S&P 500" not in msg
    assert "No trades in this period" in msg


# ── send_rh_report chart selection ────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl_with_chart", new_callable=AsyncMock)
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.generate_rh_equity_chart")
@patch("app.rh_pnl.fetch_spy_history")
@patch("app.rh_pnl.compute_spy_pct", return_value=1.0)
@patch("app.rh_pnl.rh_client")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_uses_equity_chart_when_history_available(
    mock_get_all_trades, mock_get_totals, mock_rh_client, mock_compute_spy_pct,
    mock_fetch_spy_history, mock_generate_rh_equity_chart, mock_notify, mock_notify_with_chart,
):
    """When RH equity history + SPY history are both available, prefer the
    normalized %-return equity chart over the trade-based $ P&L chart."""
    import pandas as pd
    from app.rh_pnl import send_rh_report

    mock_rh_client.get_portfolio_pct_change_async = AsyncMock(return_value=2.0)
    mock_rh_client.get_equity_history_async = AsyncMock(
        return_value=([10000.0, 10200.0], [1749700000, 1749720000])
    )
    mock_fetch_spy_history.return_value = pd.DataFrame({"Close": [600.0, 605.0]})
    mock_generate_rh_equity_chart.return_value = b"\x89PNGfakechart"

    await send_rh_report("daily")

    mock_generate_rh_equity_chart.assert_called_once()
    mock_notify_with_chart.assert_called_once()
    assert mock_notify_with_chart.call_args[0][1] == b"\x89PNGfakechart"
    mock_notify.assert_not_called()


@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl_with_chart", new_callable=AsyncMock)
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.generate_rh_pnl_chart")
@patch("app.rh_pnl.generate_rh_equity_chart")
@patch("app.rh_pnl.fetch_spy_history")
@patch("app.rh_pnl.compute_spy_pct", return_value=1.0)
@patch("app.rh_pnl.rh_client")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades")
async def test_send_rh_report_falls_back_to_pnl_chart_when_equity_history_unavailable(
    mock_get_all_trades, mock_get_totals, mock_rh_client, mock_compute_spy_pct,
    mock_fetch_spy_history, mock_generate_rh_equity_chart, mock_generate_rh_pnl_chart,
    mock_notify, mock_notify_with_chart,
):
    """If RH equity history can't be fetched, fall back to the trade-based
    cumulative $ P&L chart when there are enough trades to plot."""
    from app.rh_pnl import send_rh_report

    now = datetime.now(timezone.utc)
    mock_get_all_trades.return_value = [
        {"ts": now.isoformat(), "dollar_pnl": 50.0, "is_win": True, "ticker": "SPY"},
        {"ts": now.isoformat(), "dollar_pnl": -10.0, "is_win": False, "ticker": "SPY"},
    ]
    mock_rh_client.get_portfolio_pct_change_async = AsyncMock(return_value=2.0)
    mock_rh_client.get_equity_history_async = AsyncMock(return_value=None)
    mock_generate_rh_pnl_chart.return_value = b"\x89PNGfallback"

    await send_rh_report("daily")

    mock_generate_rh_equity_chart.assert_not_called()
    mock_generate_rh_pnl_chart.assert_called_once()
    mock_notify_with_chart.assert_called_once()
    assert mock_notify_with_chart.call_args[0][1] == b"\x89PNGfallback"


@pytest.mark.asyncio
@patch("app.rh_pnl.notify_rh_pnl_with_chart", new_callable=AsyncMock)
@patch("app.rh_pnl.notify_rh_pnl", new_callable=AsyncMock)
@patch("app.rh_pnl.generate_rh_equity_chart")
@patch("app.rh_pnl.fetch_spy_history", return_value=None)
@patch("app.rh_pnl.compute_spy_pct", return_value=1.0)
@patch("app.rh_pnl.rh_client")
@patch("app.rh_pnl.get_totals", return_value=(5, 3))
@patch("app.rh_pnl.get_all_trades", return_value=[])
async def test_send_rh_report_skips_equity_chart_when_spy_history_unavailable(
    mock_get_all_trades, mock_get_totals, mock_rh_client, mock_compute_spy_pct,
    mock_fetch_spy_history, mock_generate_rh_equity_chart, mock_notify, mock_notify_with_chart,
):
    """If RH equity history exists but SPY history doesn't, skip the equity
    chart entirely. With <2 trades, no chart should be sent at all."""
    from app.rh_pnl import send_rh_report

    mock_rh_client.get_portfolio_pct_change_async = AsyncMock(return_value=2.0)
    mock_rh_client.get_equity_history_async = AsyncMock(
        return_value=([10000.0, 10200.0], [1749700000, 1749720000])
    )

    await send_rh_report("daily")

    mock_generate_rh_equity_chart.assert_not_called()
    mock_notify_with_chart.assert_not_called()
    mock_notify.assert_called_once()
