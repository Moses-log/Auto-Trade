import os
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

import pytz

ET = pytz.timezone("America/New_York")


def test_reschedule_pending_withdrawals_does_nothing_when_none_pending():
    from app.scheduler import reschedule_pending_withdrawals
    with patch("app.scheduler.load_pending_withdrawals", return_value=[]), \
         patch("app.scheduler.scheduler") as mock_scheduler:
        reschedule_pending_withdrawals()
    mock_scheduler.add_job.assert_not_called()


def test_reschedule_pending_withdrawals_adds_a_job_per_record():
    from app.scheduler import reschedule_pending_withdrawals
    future = (datetime.now(ET) + timedelta(hours=5)).isoformat()
    records = [
        {"id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
         "requested_at": "2026-06-21T10:00:00-05:00", "run_at": future},
    ]
    with patch("app.scheduler.load_pending_withdrawals", return_value=records), \
         patch("app.scheduler.scheduler") as mock_scheduler:
        reschedule_pending_withdrawals()

    mock_scheduler.add_job.assert_called_once()
    _, kwargs = mock_scheduler.add_job.call_args
    assert kwargs["id"] == "withdrawal_wd-aaaa1111"
    assert kwargs["args"] == ["wd-aaaa1111"]
    assert kwargs["replace_existing"] is True


def test_reschedule_pending_withdrawals_uses_now_when_run_at_already_passed():
    from app.scheduler import reschedule_pending_withdrawals
    past = (datetime.now(ET) - timedelta(hours=2)).isoformat()
    records = [
        {"id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
         "requested_at": "2026-06-20T10:00:00-05:00", "run_at": past},
    ]
    with patch("app.scheduler.load_pending_withdrawals", return_value=records), \
         patch("app.scheduler.scheduler") as mock_scheduler:
        reschedule_pending_withdrawals()

    _, kwargs = mock_scheduler.add_job.call_args
    assert kwargs["run_date"] >= datetime.now(ET) - timedelta(seconds=5)
