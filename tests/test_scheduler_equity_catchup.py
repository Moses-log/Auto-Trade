import os
from datetime import datetime
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

import pytest
import pytz

ET = pytz.timezone("America/New_York")


def _at(hour: int, minute: int = 0) -> datetime:
    today = datetime.now(ET).date()
    return ET.localize(datetime(today.year, today.month, today.day, hour, minute))


@pytest.mark.asyncio
@patch("app.scheduler.record_rh_equity_snapshot", new_callable=AsyncMock)
@patch("app.scheduler.get_snapshots", return_value=[])
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler.datetime")
async def test_catch_up_records_snapshot_when_today_missing_past_4pm(
    mock_datetime, mock_was_open, mock_get_snapshots, mock_record_snapshot,
):
    from app.scheduler import catch_up_equity_snapshot

    mock_datetime.now.return_value = _at(17, 30)

    await catch_up_equity_snapshot()

    mock_record_snapshot.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.scheduler.record_rh_equity_snapshot", new_callable=AsyncMock)
@patch("app.scheduler.get_snapshots")
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler.datetime")
async def test_catch_up_skips_when_today_already_recorded(
    mock_datetime, mock_was_open, mock_get_snapshots, mock_record_snapshot,
):
    from app.scheduler import catch_up_equity_snapshot

    now = _at(17, 30)
    mock_datetime.now.return_value = now
    mock_get_snapshots.return_value = [
        {"date": now.date().isoformat(), "ts": 0, "equity": 100.0, "spy_close": 1.0},
    ]

    await catch_up_equity_snapshot()

    mock_record_snapshot.assert_not_called()


@pytest.mark.asyncio
@patch("app.scheduler.record_rh_equity_snapshot", new_callable=AsyncMock)
@patch("app.scheduler.get_snapshots", return_value=[])
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler.datetime")
async def test_catch_up_skips_before_4pm_et(
    mock_datetime, mock_was_open, mock_get_snapshots, mock_record_snapshot,
):
    from app.scheduler import catch_up_equity_snapshot

    mock_datetime.now.return_value = _at(11, 0)

    await catch_up_equity_snapshot()

    mock_record_snapshot.assert_not_called()


@pytest.mark.asyncio
@patch("app.scheduler.record_rh_equity_snapshot", new_callable=AsyncMock)
@patch("app.scheduler.get_snapshots", return_value=[])
@patch("app.scheduler.was_market_open_today", return_value=False)
@patch("app.scheduler.datetime")
async def test_catch_up_skips_on_market_holiday(
    mock_datetime, mock_was_open, mock_get_snapshots, mock_record_snapshot,
):
    from app.scheduler import catch_up_equity_snapshot

    mock_datetime.now.return_value = _at(17, 30)

    await catch_up_equity_snapshot()

    mock_record_snapshot.assert_not_called()
