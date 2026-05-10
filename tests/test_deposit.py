import os

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from unittest.mock import patch
from fastapi.testclient import TestClient

from app.investors import Deposit, Investor
from app.main import app

client = TestClient(app)
TEST_SECRET = "MY_SHARED_SECRET"


def _initial_investors():
    return [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")])
    ]


def test_deposit_rejects_wrong_secret():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        response = client.post("/deposit", json={
            "secret": "wrong-secret",
            "investor": "Moses",
            "amount": 500.0,
        })
    assert response.status_code == 401


def test_deposit_appends_to_existing_investor():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price", return_value=580.0):
                response = client.post("/deposit", json={
                    "secret": TEST_SECRET,
                    "investor": "Moses",
                    "amount": 500.0,
                })
    assert response.status_code == 200
    data = response.json()
    assert data["investor"] == "Moses"
    assert len(data["deposits"]) == 2
    assert data["deposits"][1]["amount"] == 500.0
    assert data["deposits"][1]["entry_spy"] == 580.0


def test_deposit_uses_provided_spy_price_and_skips_alpaca_call():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price") as mock_price:
                response = client.post("/deposit", json={
                    "secret": TEST_SECRET,
                    "investor": "Moses",
                    "amount": 500.0,
                    "spy_price": 595.0,
                })
    assert response.status_code == 200
    mock_price.assert_not_called()
    assert response.json()["deposits"][1]["entry_spy"] == 595.0


def test_deposit_creates_new_investor_when_name_not_found():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price", return_value=580.0):
                response = client.post("/deposit", json={
                    "secret": TEST_SECRET,
                    "investor": "Alice",
                    "amount": 1000.0,
                })
    assert response.status_code == 200
    data = response.json()
    assert data["investor"] == "Alice"
    assert len(data["deposits"]) == 1
    assert data["deposits"][0]["amount"] == 1000.0


def test_deposit_matches_investor_name_case_insensitively():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price", return_value=580.0):
                response = client.post("/deposit", json={
                    "secret": TEST_SECRET,
                    "investor": "moses",
                    "amount": 200.0,
                })
    assert response.status_code == 200
    assert response.json()["investor"] == "Moses"


def test_deposit_returns_502_when_spy_price_unavailable():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.get_latest_price", return_value=None):
            response = client.post("/deposit", json={
                "secret": TEST_SECRET,
                "investor": "Moses",
                "amount": 500.0,
            })
    assert response.status_code == 502


def test_deposit_rejects_zero_amount():
    response = client.post("/deposit", json={
        "secret": TEST_SECRET,
        "investor": "Moses",
        "amount": 0.0,
    })
    assert response.status_code == 422
