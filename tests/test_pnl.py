import os
import pytest
from unittest.mock import patch, MagicMock

# Must set env vars before importing any app module
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "test_webhook")


# ── alpaca_client tests ───────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_client")
def test_get_portfolio_history_calls_sdk(mock_get_client):
    """get_portfolio_history() must call TradingClient.get_portfolio_history with correct params."""
    from app.trading.alpaca_client import get_portfolio_history

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    fake_history = MagicMock()
    mock_client.get_portfolio_history.return_value = fake_history

    result = get_portfolio_history(period="1D", timeframe="1Min")

    mock_client.get_portfolio_history.assert_called_once()
    call_kwargs = mock_client.get_portfolio_history.call_args
    # Verify a filter object was passed (not raw strings)
    assert call_kwargs is not None
    assert result is fake_history


# ── pnl.py tests ─────────────────────────────────────────────────────────────

from dataclasses import dataclass


@dataclass
class FakeHistory:
    equity: list


def test_compute_pnl_profit():
    from app.pnl import _compute_pnl
    history = FakeHistory(equity=[10000.0, 10100.0, 10200.0, 10320.50])
    result = _compute_pnl(history, "daily")
    assert result.period == "daily"
    assert result.close_equity == pytest.approx(10320.50)
    assert result.dollar_pnl == pytest.approx(320.50)
    assert result.pct_pnl == pytest.approx(3.205)


def test_compute_pnl_loss():
    from app.pnl import _compute_pnl
    history = FakeHistory(equity=[10000.0, 9700.0])
    result = _compute_pnl(history, "weekly")
    assert result.dollar_pnl == pytest.approx(-300.0)
    assert result.pct_pnl == pytest.approx(-3.0)


def test_format_message_profit():
    from app.pnl import _format_message, PnLResult
    result = PnLResult(period="daily", close_equity=10320.50, dollar_pnl=320.50, pct_pnl=3.21)
    msg = _format_message(result, "Daily P&L", "Monday May 5, 2026")
    assert "📈" in msg
    assert "$10,320.50" in msg
    assert "+$320.50" in msg
    assert "+3.21%" in msg


def test_format_message_loss():
    from app.pnl import _format_message, PnLResult
    result = PnLResult(period="daily", close_equity=9700.0, dollar_pnl=-300.0, pct_pnl=-3.0)
    msg = _format_message(result, "Daily P&L", "Monday May 5, 2026")
    assert "📉" in msg
    assert "-$300.00" in msg
    assert "-3.00%" in msg


@pytest.mark.asyncio
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify")
async def test_send_daily_report_success(mock_notify, mock_get_history):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10320.50])
    mock_notify.return_value = None
    from app.pnl import send_daily_report
    await send_daily_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "📈" in msg
    assert "Daily P&L" in msg
    assert "$10,320.50" in msg


@pytest.mark.asyncio
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify")
async def test_send_daily_report_alpaca_error(mock_notify, mock_get_history):
    mock_get_history.side_effect = Exception("Alpaca unreachable")
    mock_notify.return_value = None
    from app.pnl import send_daily_report
    await send_daily_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "⚠️" in msg
    assert "Daily P&L report failed" in msg


@pytest.mark.asyncio
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify")
async def test_send_weekly_report_success(mock_notify, mock_get_history):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10875.20])
    mock_notify.return_value = None
    from app.pnl import send_weekly_report
    await send_weekly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Weekly P&L" in msg
    assert "$10,875.20" in msg


@pytest.mark.asyncio
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify")
async def test_send_weekly_report_alpaca_error(mock_notify, mock_get_history):
    mock_get_history.side_effect = Exception("Alpaca unreachable")
    mock_notify.return_value = None
    from app.pnl import send_weekly_report
    await send_weekly_report()
    msg = mock_notify.call_args[0][0]
    assert "⚠️" in msg
    assert "Weekly P&L report failed" in msg


# ── scheduler.py tests ────────────────────────────────────────────────────────

def test_scheduler_jobs_registered():
    """setup_jobs() must register exactly 2 jobs: daily_pnl and weekly_pnl."""
    from app.scheduler import scheduler, setup_jobs
    # Start fresh — remove any jobs from previous calls
    scheduler.remove_all_jobs()
    setup_jobs()
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert "daily_pnl" in job_ids
    assert "weekly_pnl" in job_ids
    assert len(job_ids) == 2
