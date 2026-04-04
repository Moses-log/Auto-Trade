"""
test_webhook.py — Integration-style tests for the webhook endpoint.

Run with:
    pytest tests/ -v

These tests use FastAPI's TestClient (synchronous wrapper) and mock the
Alpaca client so no real orders are placed. Set ALPACA_API_KEY etc. to
dummy values — the tests never hit the real Alpaca API.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Provide dummy env vars before importing the app ──────────────────────────
os.environ.setdefault("ALPACA_API_KEY",    "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET",    "MY_SHARED_SECRET")

from app.main import app  # noqa: E402 — must come after env setup

client = TestClient(app)

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _load_sample(action: str) -> dict:
    path = os.path.join(os.path.dirname(__file__), "sample_payloads.json")
    with open(path) as f:
        return json.load(f)[action]


def _mock_order(side="buy", qty="10", symbol="AAPL"):
    order = MagicMock()
    order.id     = "mock-order-id-123"
    order.symbol = symbol
    order.side   = side
    order.qty    = qty
    order.order_type = "market"
    order.status = "accepted"
    return order


# ── Health check ──────────────────────────────────────────────────────────────

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_wrong_secret_rejected():
    payload = _load_sample("buy")
    payload["secret"] = "WRONG_SECRET"
    r = client.post("/webhook", json=payload)
    assert r.status_code == 401


def test_missing_secret_rejected():
    payload = _load_sample("buy")
    del payload["secret"]
    r = client.post("/webhook", json=payload)
    # secret field missing → Pydantic error or auth rejection
    assert r.status_code in (401, 422)


# ── BUY ───────────────────────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_client")
def test_buy_order(mock_get_client):
    mock_client = MagicMock()
    mock_client.submit_order.return_value = _mock_order(side="buy")
    mock_get_client.return_value = mock_client

    payload = _load_sample("buy")
    r = client.post("/webhook", json=payload)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["result"]["orders"][0]["side"] == "buy"
    mock_client.submit_order.assert_called_once()


# ── SELL ──────────────────────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_client")
def test_sell_order(mock_get_client):
    mock_client = MagicMock()
    mock_client.submit_order.return_value = _mock_order(side="sell")
    mock_get_client.return_value = mock_client

    payload = _load_sample("sell")
    r = client.post("/webhook", json=payload)

    assert r.status_code == 200
    assert r.json()["result"]["orders"][0]["side"] == "sell"


# ── CLOSE LONG ────────────────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_client")
def test_close_long_with_position(mock_get_client):
    mock_position = MagicMock()
    mock_position.qty  = "5"
    mock_position.side = "long"

    mock_client = MagicMock()
    mock_client.get_open_position.return_value = mock_position
    mock_client.close_position.return_value = _mock_order(side="sell", qty="5", symbol="SPY")
    mock_get_client.return_value = mock_client

    payload = _load_sample("close_long")
    r = client.post("/webhook", json=payload)

    assert r.status_code == 200
    mock_client.close_position.assert_called_once_with("SPY")


@patch("app.trading.alpaca_client.get_client")
def test_close_long_no_position(mock_get_client):
    from alpaca.common.exceptions import APIError
    mock_client = MagicMock()
    mock_client.get_open_position.side_effect = APIError(
        '{"code":40410000,"message":"position does not exist"}'
    )
    mock_get_client.return_value = mock_client

    payload = _load_sample("close_long")
    payload["order_id"] = "order_003_no_position"  # unique to avoid idempotency collision
    r = client.post("/webhook", json=payload)

    assert r.status_code == 200
    assert r.json()["result"]["note"] == "No long position to close."


# ── Idempotency ───────────────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_client")
def test_duplicate_alert_rejected(mock_get_client):
    mock_client = MagicMock()
    mock_client.submit_order.return_value = _mock_order()
    mock_get_client.return_value = mock_client

    # Use a unique order_id to avoid interference from other tests
    payload = _load_sample("buy")
    payload["order_id"] = "dedup_test_order_999"

    r1 = client.post("/webhook", json=payload)
    r2 = client.post("/webhook", json=payload)

    assert r1.status_code == 200
    assert r1.json()["status"] == "ok"
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"
    # Alpaca should only be called once
    assert mock_client.submit_order.call_count == 1


# ── Invalid payloads ──────────────────────────────────────────────────────────

def test_invalid_action_rejected():
    payload = _load_sample("buy")
    payload["action"] = "nuke_portfolio"
    r = client.post("/webhook", json=payload)
    assert r.status_code == 422


def test_non_json_body_rejected():
    r = client.post("/webhook", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_missing_ticker_rejected():
    payload = _load_sample("buy")
    del payload["ticker"]
    r = client.post("/webhook", json=payload)
    assert r.status_code == 422
