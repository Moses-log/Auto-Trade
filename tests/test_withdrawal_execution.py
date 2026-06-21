import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

import pytz

from app.investors import Deposit, Investor

_CT = pytz.timezone("America/Chicago")


def _moses(deposits_amount=2000.0, entry_spy=707.0):
    return Investor(name="Moses", deposits=[
        Deposit(amount=deposits_amount, entry_spy=entry_spy, date="2026-05-09")
    ])


@pytest.mark.asyncio
async def test_schedule_withdrawal_rejects_non_positive_amount():
    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError
    with pytest.raises(WithdrawalValidationError, match="positive"):
        await schedule_withdrawal("Moses", 0.0)


@pytest.mark.asyncio
async def test_schedule_withdrawal_rejects_unknown_investor():
    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError
    with patch("app.withdrawal_execution.load_investors", return_value=[]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20):
        with pytest.raises(WithdrawalValidationError, match="not found"):
            await schedule_withdrawal("Ghost", 500.0)


@pytest.mark.asyncio
async def test_schedule_withdrawal_rejects_amount_exceeding_equity():
    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError
    inv = _moses(deposits_amount=300.0)
    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20):
        with pytest.raises(WithdrawalValidationError, match="exceeds"):
            await schedule_withdrawal("Moses", 5000.0)


@pytest.mark.asyncio
async def test_schedule_withdrawal_saves_pending_and_adds_scheduler_job():
    from app.withdrawal_execution import schedule_withdrawal
    inv = _moses()
    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.save_pending_withdrawal") as mock_save, \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler:
        record = await schedule_withdrawal("moses", 500.0)

    assert record["investor"] == "Moses"  # canonical case from the stored Investor, not user input
    assert record["amount"] == 500.0
    assert record["id"].startswith("wd-")
    mock_save.assert_called_once()
    mock_scheduler.add_job.assert_called_once()
    _, kwargs = mock_scheduler.add_job.call_args
    assert kwargs["id"] == f"withdrawal_{record['id']}"
    assert kwargs["args"] == [record["id"]]


@pytest.mark.asyncio
async def test_schedule_withdrawal_run_at_respects_delay_setting():
    from app.withdrawal_execution import schedule_withdrawal
    inv = _moses()
    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.save_pending_withdrawal"), \
         patch("app.withdrawal_execution.scheduler"), \
         patch("app.withdrawal_execution.settings") as mock_settings:
        mock_settings.withdrawal_delay_hours = 24
        before = datetime.now(_CT)
        record = await schedule_withdrawal("Moses", 500.0)
        run_at = datetime.fromisoformat(record["run_at"])

    assert run_at - before >= timedelta(hours=23, minutes=59)
    assert run_at - before <= timedelta(hours=24, minutes=1)


@pytest.mark.asyncio
async def test_execute_pending_withdrawal_writes_to_investors_and_audits_executed():
    from app.withdrawal_execution import execute_pending_withdrawal
    inv = _moses()
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.save_investors") as mock_save, \
         patch("app.withdrawal_execution.remove_pending_withdrawal") as mock_remove, \
         patch("app.withdrawal_execution.append_withdrawal_audit") as mock_audit, \
         patch("app.withdrawal_execution.notify_investors") as mock_notify, \
         patch("app.withdrawal_execution.push_backup") as mock_backup:
        mock_notify.return_value = _async_none()
        mock_backup.return_value = _async_none()
        await execute_pending_withdrawal("wd-aaaa1111")

    mock_save.assert_called_once()
    assert len(inv.withdrawals) == 1
    assert inv.withdrawals[0].proceeds == 500.0
    mock_remove.assert_called_once_with("wd-aaaa1111")
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["status"] == "executed"


@pytest.mark.asyncio
async def test_execute_pending_withdrawal_returns_silently_when_record_missing():
    from app.withdrawal_execution import execute_pending_withdrawal
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=None), \
         patch("app.withdrawal_execution.save_investors") as mock_save:
        await execute_pending_withdrawal("wd-gone")
    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_execute_pending_withdrawal_audits_failed_when_equity_insufficient():
    from app.withdrawal_execution import execute_pending_withdrawal
    inv = _moses(deposits_amount=100.0)  # not enough left for a $500 withdrawal
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.save_investors") as mock_save, \
         patch("app.withdrawal_execution.remove_pending_withdrawal") as mock_remove, \
         patch("app.withdrawal_execution.append_withdrawal_audit") as mock_audit, \
         patch("app.withdrawal_execution.notify_investors") as mock_notify:
        mock_notify.return_value = _async_none()
        await execute_pending_withdrawal("wd-aaaa1111")

    mock_save.assert_not_called()
    mock_remove.assert_called_once_with("wd-aaaa1111")
    assert mock_audit.call_args.kwargs["status"] == "failed"
    assert "reason" in mock_audit.call_args.kwargs


@pytest.mark.asyncio
async def test_cancel_pending_withdrawal_removes_job_and_record_and_audits():
    from app.withdrawal_execution import cancel_pending_withdrawal
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler, \
         patch("app.withdrawal_execution.remove_pending_withdrawal") as mock_remove, \
         patch("app.withdrawal_execution.append_withdrawal_audit") as mock_audit:
        result = await cancel_pending_withdrawal("wd-aaaa1111")

    mock_scheduler.remove_job.assert_called_once_with("withdrawal_wd-aaaa1111")
    mock_remove.assert_called_once_with("wd-aaaa1111")
    assert mock_audit.call_args.kwargs["status"] == "canceled"
    assert result["investor"] == "Moses"


@pytest.mark.asyncio
async def test_cancel_pending_withdrawal_raises_when_not_found():
    from app.withdrawal_execution import cancel_pending_withdrawal, WithdrawalNotFoundError
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=None):
        with pytest.raises(WithdrawalNotFoundError):
            await cancel_pending_withdrawal("wd-missing")


@pytest.mark.asyncio
async def test_cancel_pending_withdrawal_succeeds_even_if_job_already_gone():
    from app.withdrawal_execution import cancel_pending_withdrawal
    from apscheduler.jobstores.base import JobLookupError
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler, \
         patch("app.withdrawal_execution.remove_pending_withdrawal"), \
         patch("app.withdrawal_execution.append_withdrawal_audit"):
        mock_scheduler.remove_job.side_effect = JobLookupError("withdrawal_wd-aaaa1111")
        result = await cancel_pending_withdrawal("wd-aaaa1111")

    assert result["id"] == "wd-aaaa1111"


def _async_none():
    async def _coro():
        return None
    return _coro()
