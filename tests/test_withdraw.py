import os
import pytest
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from fastapi.testclient import TestClient

from app.investors import Deposit, Investor
from app.main import app

client = TestClient(app)
TEST_SECRET = "MY_SHARED_SECRET"


def _initial_investors():
    return [
        Investor(name="Moses", deposits=[Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09")])
    ]


def test_withdraw_rejects_wrong_secret():
    response = client.post("/withdraw", json={
        "secret": "wrong-secret",
        "investor": "Moses",
        "amount": 500.0,
    })
    assert response.status_code == 401


def test_withdraw_rejects_malformed_json():
    response = client.post(
        "/withdraw",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_withdraw_schedules_instead_of_writing_immediately():
    with patch("app.withdrawal_execution.load_investors", return_value=_initial_investors()), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.save_pending_withdrawal") as mock_save_pending, \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler:
        response = client.post("/withdraw", json={
            "secret": TEST_SECRET,
            "investor": "Moses",
            "amount": 500.0,
        })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "scheduled"
    assert data["investor"] == "Moses"
    assert data["amount"] == 500.0
    assert data["id"].startswith("wd-")
    # Proves this went through the delay mechanism, not a direct investors.json write:
    mock_save_pending.assert_called_once()
    mock_scheduler.add_job.assert_called_once()


def test_withdraw_returns_400_when_amount_exceeds_equity():
    with patch("app.withdrawal_execution.load_investors", return_value=_initial_investors()), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20):
        response = client.post("/withdraw", json={
            "secret": TEST_SECRET,
            "investor": "Moses",
            "amount": 50000.0,
        })
    assert response.status_code == 400
    assert "exceeds" in response.json()["error"]


def test_withdraw_returns_400_when_investor_not_found():
    with patch("app.withdrawal_execution.load_investors", return_value=_initial_investors()), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20):
        response = client.post("/withdraw", json={
            "secret": TEST_SECRET,
            "investor": "Ghost",
            "amount": 500.0,
        })
    assert response.status_code == 400
    assert "not found" in response.json()["error"]


def test_withdraw_rejects_zero_amount():
    response = client.post("/withdraw", json={
        "secret": TEST_SECRET,
        "investor": "Moses",
        "amount": 0.0,
    })
    assert response.status_code == 422
