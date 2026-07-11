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


@pytest.mark.asyncio
@patch("app.scheduler.run_monthly_rebalance", new_callable=AsyncMock)
@patch("app.scheduler._rebalance_already_completed_this_month", return_value=True)
@patch("app.scheduler.is_first_trading_day_of", return_value=True)
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_rebalance_skips_when_log_shows_already_completed(
    mock_date, mock_was_open, mock_is_first, mock_already_completed, mock_rebalance,
):
    """is_first_trading_day_of() fails open (returns True) on a transient
    Alpaca API error — if day 1 already completed for real, the log-based
    idempotency guard must still block a day 2-4 duplicate run."""
    from app.scheduler import _claude_monthly_rebalance
    mock_date.today.return_value = date(2026, 7, 3)

    await _claude_monthly_rebalance()

    mock_rebalance.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.scheduler.run_monthly_rebalance", None)
@patch("app.scheduler.is_first_trading_day_of", return_value=True)
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_rebalance_skips_gracefully_when_claude_manager_failed_to_import(
    mock_date, mock_was_open, mock_is_first,
):
    """If app.claude_manager raised at startup import time, run_monthly_rebalance
    is None — the job must skip cleanly instead of crashing on None()."""
    from app.scheduler import _claude_monthly_rebalance
    mock_date.today.return_value = date(2026, 7, 1)

    await _claude_monthly_rebalance()  # must not raise


def test_rebalance_already_completed_detects_finalized_status(tmp_path, monkeypatch):
    import json
    from datetime import date as real_date

    log_path = tmp_path / "claude_rebalance_log.json"
    log_path.write_text(json.dumps([
        {"timestamp": f"{real_date.today().replace(day=1).isoformat()}T09:35:00", "status": "completed"},
    ]))
    monkeypatch.setattr("app.scheduler._REBALANCE_LOG_PATH", str(log_path))

    from app.scheduler import _rebalance_already_completed_this_month
    assert _rebalance_already_completed_this_month() is True


def test_rebalance_already_completed_ignores_pre_decision_failure(tmp_path, monkeypatch):
    import json
    from datetime import date as real_date

    log_path = tmp_path / "claude_rebalance_log.json"
    log_path.write_text(json.dumps([
        {"timestamp": f"{real_date.today().replace(day=1).isoformat()}T09:35:00", "status": "failed_fetch"},
    ]))
    monkeypatch.setattr("app.scheduler._REBALANCE_LOG_PATH", str(log_path))

    from app.scheduler import _rebalance_already_completed_this_month
    assert _rebalance_already_completed_this_month() is False
