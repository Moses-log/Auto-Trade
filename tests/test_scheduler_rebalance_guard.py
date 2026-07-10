import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@patch("app.scheduler.run_monthly_rebalance", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of", return_value=True)
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_rebalance_runs_when_first_trading_day(
    mock_date, mock_was_open, mock_is_first, mock_rebalance,
):
    from app.scheduler import _claude_monthly_rebalance
    mock_date.today.return_value = date(2026, 7, 1)

    await _claude_monthly_rebalance()

    mock_rebalance.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.scheduler.run_monthly_rebalance", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of", return_value=False)
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_rebalance_skips_when_earlier_day_already_ran(
    mock_date, mock_was_open, mock_is_first, mock_rebalance,
):
    """Cron fired on day 3, but day 1 (a Saturday's Monday makeup) already ran."""
    from app.scheduler import _claude_monthly_rebalance
    mock_date.today.return_value = date(2026, 7, 3)

    await _claude_monthly_rebalance()

    mock_rebalance.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.scheduler.run_monthly_rebalance", new_callable=AsyncMock)
@patch("app.scheduler.was_market_open_today", return_value=False)
@patch("app.scheduler._date")
async def test_rebalance_skips_on_holiday(mock_date, mock_was_open, mock_rebalance):
    from app.scheduler import _claude_monthly_rebalance
    mock_date.today.return_value = date(2026, 7, 1)

    await _claude_monthly_rebalance()

    mock_rebalance.assert_not_awaited()
