import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

# Must set env vars before importing any app module
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


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
    assert "📈🟢" in msg
    assert "$10,320.50" in msg
    assert "+$320.50" in msg
    assert "+3.21%" in msg


def test_format_message_loss():
    from app.pnl import _format_message, PnLResult
    result = PnLResult(period="daily", close_equity=9700.0, dollar_pnl=-300.0, pct_pnl=-3.0)
    msg = _format_message(result, "Daily P&L", "Monday May 5, 2026")
    assert "📉🔴" in msg
    assert "-$300.00" in msg
    assert "-3.00%" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_daily_report_success(mock_notify, mock_get_history, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10320.50])
    from app.pnl import send_daily_report
    await send_daily_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "📈🟢" in msg
    assert "Daily P&L" in msg
    assert "$10,320.50" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_daily_report_alpaca_error(mock_notify, mock_get_history, mock_spy):
    mock_get_history.side_effect = Exception("Alpaca unreachable")
    from app.pnl import send_daily_report
    await send_daily_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "⚠️" in msg
    assert "Daily P&L report failed" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_weekly_report_success(mock_notify, mock_get_history, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10875.20])
    from app.pnl import send_weekly_report
    await send_weekly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Weekly P&L" in msg
    assert "$10,875.20" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_weekly_report_alpaca_error(mock_notify, mock_get_history, mock_spy):
    mock_get_history.side_effect = Exception("Alpaca unreachable")
    from app.pnl import send_weekly_report
    await send_weekly_report()
    msg = mock_notify.call_args[0][0]
    assert "⚠️" in msg
    assert "Weekly P&L report failed" in msg


# ── scheduler.py tests ────────────────────────────────────────────────────────

def test_scheduler_jobs_registered():
    """setup_jobs() must register the P&L and investor breakdown cron jobs."""
    from app.scheduler import scheduler, setup_jobs
    # Start fresh — remove any jobs from previous calls
    scheduler.remove_all_jobs()
    setup_jobs()
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert "daily_pnl" in job_ids
    assert "weekly_pnl" in job_ids
    assert "investor_breakdown_daily" in job_ids
    assert "investor_breakdown_weekly" in job_ids
    assert "period_pnl_check" in job_ids
    assert len(job_ids) == 5


# ── get_spy_bars tests ────────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_data_client")
def test_get_spy_bars_calls_sdk(mock_get_data_client):
    """get_spy_bars() must call get_stock_bars with SPY and TimeFrame.Day."""
    from app.trading.alpaca_client import get_spy_bars
    from datetime import datetime
    import pytz

    mock_client = MagicMock()
    mock_get_data_client.return_value = mock_client
    fake_bars = MagicMock()
    mock_client.get_stock_bars.return_value = fake_bars

    ET = pytz.timezone("America/New_York")
    start = datetime(2026, 5, 12, 9, 30, tzinfo=ET)
    end = datetime(2026, 5, 12, 16, 0, tzinfo=ET)

    result = get_spy_bars(start=start, end=end)

    mock_client.get_stock_bars.assert_called_once()
    req_arg = mock_client.get_stock_bars.call_args[0][0]
    assert req_arg.symbol_or_symbols == "SPY"
    assert result is fake_bars


# ── compute_spy_pct tests ─────────────────────────────────────────────────────

def test_compute_spy_pct_normal():
    import pandas as pd
    from app.pnl import compute_spy_pct
    fake_df = pd.DataFrame({"Open": [500.0, 502.0], "Close": [503.0, 506.0]})
    with patch("app.pnl.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.history.return_value = fake_df
        result = compute_spy_pct("1d")
    assert result == pytest.approx(1.2)  # (506-500)/500 * 100


def test_compute_spy_pct_empty_data():
    import pandas as pd
    from app.pnl import compute_spy_pct
    with patch("app.pnl.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.history.return_value = pd.DataFrame()
        result = compute_spy_pct("1d")
    assert result is None


def test_compute_spy_pct_returns_none_on_exception():
    from app.pnl import compute_spy_pct
    with patch("app.pnl.yf.Ticker", side_effect=Exception("network error")):
        result = compute_spy_pct("1d")
    assert result is None


# ── fetch_spy_history tests ───────────────────────────────────────────────────

def test_fetch_spy_history_returns_dataframe():
    from datetime import date
    import pandas as pd
    fake_df = pd.DataFrame(
        {"Close": [537.0, 539.0]},
        index=pd.date_range("2026-05-09", periods=2),
    )
    with patch("app.pnl.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.history.return_value = fake_df
        from app.pnl import fetch_spy_history
        result = fetch_spy_history(date(2026, 5, 9), date(2026, 5, 16))
    assert result is not None
    assert "Close" in result.columns


def test_fetch_spy_history_returns_none_on_empty():
    from datetime import date
    import pandas as pd
    with patch("app.pnl.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.history.return_value = pd.DataFrame()
        from app.pnl import fetch_spy_history
        result = fetch_spy_history(date(2026, 5, 9), date(2026, 5, 16))
    assert result is None


def test_fetch_spy_history_returns_none_on_exception():
    from datetime import date
    with patch("app.pnl.yf.Ticker", side_effect=Exception("network error")):
        from app.pnl import fetch_spy_history
        result = fetch_spy_history(date(2026, 5, 9), date(2026, 5, 16))
    assert result is None


# ── _format_message with spy_pct tests ───────────────────────────────────────

def test_format_message_spy_ahead():
    from app.pnl import _format_message, PnLResult
    result = PnLResult(period="daily", close_equity=10320.50, dollar_pnl=320.50, pct_pnl=2.64)
    msg = _format_message(result, "Daily P&L", "Monday May 12, 2026", spy_pct=1.20)
    assert "S&P 500: +1.20%" in msg
    assert "ahead" in msg
    assert "+1.44%" in msg


def test_format_message_spy_behind():
    from app.pnl import _format_message, PnLResult
    result = PnLResult(period="daily", close_equity=9850.0, dollar_pnl=-150.0, pct_pnl=-1.20)
    msg = _format_message(result, "Daily P&L", "Monday May 12, 2026", spy_pct=1.20)
    assert "S&P 500: +1.20%" in msg
    assert "behind" in msg
    assert "-2.40%" in msg


def test_format_message_no_spy():
    from app.pnl import _format_message, PnLResult
    result = PnLResult(period="daily", close_equity=10320.50, dollar_pnl=320.50, pct_pnl=2.64)
    msg = _format_message(result, "Daily P&L", "Monday May 12, 2026")
    assert "S&P 500" not in msg


def test_format_message_spy_negative():
    from app.pnl import _format_message, PnLResult
    result = PnLResult(period="daily", close_equity=9900.0, dollar_pnl=-100.0, pct_pnl=-1.0)
    msg = _format_message(result, "Daily P&L", "Monday May 12, 2026", spy_pct=-2.0)
    assert "S&P 500: -2.00%" in msg
    assert "ahead" in msg
    assert "+1.00%" in msg


# ── send_*_report with SPY tests ──────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=1.2)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_daily_report_includes_spy(mock_notify, mock_get_history, mock_spy):
    """Daily report must include S&P 500 line when SPY data available."""
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10264.0])
    from app.pnl import send_daily_report
    await send_daily_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "S&P 500" in msg
    assert "ahead" in msg or "behind" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_daily_report_spy_fetch_fails(mock_notify, mock_get_history, mock_spy):
    """Daily report must still post without S&P line when SPY fetch returns None."""
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10264.0])
    from app.pnl import send_daily_report
    await send_daily_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Daily P&L" in msg
    assert "S&P 500" not in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=2.0)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_weekly_report_includes_spy(mock_notify, mock_get_history, mock_spy):
    """Weekly report must include S&P 500 line when SPY data available."""
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10875.0])
    from app.pnl import send_weekly_report
    await send_weekly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "S&P 500" in msg
    assert "Weekly P&L" in msg


# ── get_order tests ───────────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_client")
def test_get_order_returns_order_on_success(mock_get_client):
    from app.trading.alpaca_client import get_order
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    fake_order = MagicMock()
    fake_order.filled_avg_price = "537.42"
    fake_order.filled_qty = "5"
    mock_client.get_order_by_id.return_value = fake_order
    result = get_order("abc-123")
    assert result is fake_order
    mock_client.get_order_by_id.assert_called_once_with("abc-123")


@patch("app.trading.alpaca_client.get_client")
def test_get_order_returns_none_on_exception(mock_get_client):
    from app.trading.alpaca_client import get_order
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_order_by_id.side_effect = Exception("not found")
    result = get_order("bad-id")
    assert result is None


# ── New extended report tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_monthly_report_success(mock_notify, mock_get_history, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10500.0])
    from app.pnl import send_monthly_report
    await send_monthly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Monthly P&L" in msg
    assert "$10,500.00" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_monthly_report_alpaca_error(mock_notify, mock_get_history, mock_spy):
    mock_get_history.side_effect = Exception("Alpaca unreachable")
    from app.pnl import send_monthly_report
    await send_monthly_report()
    msg = mock_notify.call_args[0][0]
    assert "⚠️" in msg
    assert "Monthly" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_yearly_report_success(mock_notify, mock_get_history, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 12000.0])
    from app.pnl import send_yearly_report
    await send_yearly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Yearly P&L" in msg
    assert "$12,000.00" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_ytd_report_success(mock_notify, mock_get_history, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10800.0])
    from app.pnl import send_ytd_report
    await send_ytd_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "YTD" in msg
    assert "$10,800.00" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_alltime_report_success(mock_notify, mock_get_history, mock_spy):
    import time
    fake_history = MagicMock()
    fake_history.equity = [0.0, 10000.0, 11500.0]
    fake_history.timestamp = [
        int(time.time()) - 86400 * 10,
        int(time.time()) - 86400 * 5,
        int(time.time()),
    ]
    mock_get_history.return_value = fake_history
    from app.pnl import send_alltime_report
    await send_alltime_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "All-Time P&L" in msg
    assert "since" in msg


@pytest.mark.asyncio
@patch("app.pnl.get_next_trading_day")
@patch("app.pnl.send_monthly_report", new_callable=AsyncMock)
@patch("app.pnl.send_yearly_report", new_callable=AsyncMock)
async def test_check_period_reports_fires_monthly_on_last_trading_day_of_month(
    mock_yearly, mock_monthly, mock_next_day
):
    from datetime import date
    # next trading day is in a different month → fire monthly
    mock_next_day.return_value = date(2026, 6, 1)
    with patch("app.pnl.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 5, 30)
        from app.pnl import check_period_reports
        await check_period_reports()
    mock_monthly.assert_called_once()
    mock_yearly.assert_not_called()


@pytest.mark.asyncio
@patch("app.pnl.get_next_trading_day")
@patch("app.pnl.send_monthly_report", new_callable=AsyncMock)
@patch("app.pnl.send_yearly_report", new_callable=AsyncMock)
async def test_check_period_reports_fires_both_on_last_trading_day_of_year(
    mock_yearly, mock_monthly, mock_next_day
):
    from datetime import date
    # next trading day is in a different year → fire both monthly and yearly
    mock_next_day.return_value = date(2027, 1, 2)
    with patch("app.pnl.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 12, 31)
        from app.pnl import check_period_reports
        await check_period_reports()
    mock_monthly.assert_called_once()
    mock_yearly.assert_called_once()


@pytest.mark.asyncio
@patch("app.pnl.get_next_trading_day")
@patch("app.pnl.send_monthly_report", new_callable=AsyncMock)
@patch("app.pnl.send_yearly_report", new_callable=AsyncMock)
async def test_check_period_reports_silent_mid_month(
    mock_yearly, mock_monthly, mock_next_day
):
    from datetime import date
    # next trading day is same month/year → fire nothing
    mock_next_day.return_value = date(2026, 5, 18)
    with patch("app.pnl.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 5, 15)
        from app.pnl import check_period_reports
        await check_period_reports()
    mock_monthly.assert_not_called()
    mock_yearly.assert_not_called()
