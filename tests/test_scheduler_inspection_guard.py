import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@patch("app.scheduler.run_weekly_inspection", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of")
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_inspection_runs_on_first_trading_day_of_week(
    mock_date, mock_was_open, mock_is_first, mock_inspection,
):
    from app.scheduler import _weekly_inspection
    monday = date(2026, 7, 6)  # a Monday, not the 1st of a month
    mock_date.today.return_value = monday
    # is_first_trading_day_of(week_start) -> True (first trading day of week)
    # is_first_trading_day_of(month_start) -> False (not the rebalance day)
    mock_is_first.side_effect = [True, False]

    await _weekly_inspection()

    mock_inspection.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.scheduler.run_weekly_inspection", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of")
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_inspection_skips_when_already_ran_this_week(
    mock_date, mock_was_open, mock_is_first, mock_inspection,
):
    from app.scheduler import _weekly_inspection
    wednesday = date(2026, 7, 8)
    mock_date.today.return_value = wednesday
    mock_is_first.return_value = False  # Monday or Tuesday already traded this week

    await _weekly_inspection()

    mock_inspection.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.scheduler.run_weekly_inspection", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of")
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_inspection_skips_when_coincides_with_monthly_rebalance(
    mock_date, mock_was_open, mock_is_first, mock_inspection,
):
    from app.scheduler import _weekly_inspection
    first_of_month = date(2026, 9, 1)  # a Tuesday that's also the 1st
    mock_date.today.return_value = first_of_month
    # first trading day of week -> True, but also first trading day of month -> True
    mock_is_first.side_effect = [True, True]

    await _weekly_inspection()

    mock_inspection.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.scheduler.run_weekly_inspection", new_callable=AsyncMock)
@patch("app.scheduler.was_market_open_today", return_value=False)
@patch("app.scheduler._date")
async def test_inspection_skips_on_holiday(mock_date, mock_was_open, mock_inspection):
    from app.scheduler import _weekly_inspection
    mock_date.today.return_value = date(2026, 7, 6)

    await _weekly_inspection()

    mock_inspection.assert_not_awaited()
