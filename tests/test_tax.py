import os
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


# ── RH tax summary ────────────────────────────────────────────────────────────

def test_rh_tax_summary_basic():
    trades = [
        {"ts": "2025-03-10T14:00:00+00:00", "ticker": "SPY", "dollar_pnl": 100.0, "is_win": True},
        {"ts": "2025-05-15T14:00:00+00:00", "ticker": "SPY", "dollar_pnl": -40.0, "is_win": False},
        {"ts": "2025-08-20T14:00:00+00:00", "ticker": "SPY", "dollar_pnl": 200.0, "is_win": True},
        {"ts": "2024-12-31T23:59:00+00:00", "ticker": "SPY", "dollar_pnl": 999.0, "is_win": True},  # prior year
    ]
    with patch("app.rh_trade_record.get_all_trades", return_value=trades):
        from app.tax import compute_rh_tax_summary
        result = compute_rh_tax_summary(2025)

    assert result["total_trades"] == 3
    assert result["win_count"] == 2
    assert result["loss_count"] == 1
    assert abs(result["short_term_gains"] - 300.0) < 0.01
    assert abs(result["short_term_losses"] - (-40.0)) < 0.01
    assert abs(result["short_term_net"] - 260.0) < 0.01


def test_rh_tax_summary_no_trades():
    with patch("app.rh_trade_record.get_all_trades", return_value=[]):
        from app.tax import compute_rh_tax_summary
        result = compute_rh_tax_summary(2025)

    assert result["total_trades"] == 0
    assert result["short_term_net"] == 0.0


def test_rh_tax_summary_filters_other_years():
    trades = [
        {"ts": "2024-06-01T00:00:00+00:00", "ticker": "SPY", "dollar_pnl": 500.0, "is_win": True},
        {"ts": "2023-01-15T00:00:00+00:00", "ticker": "SPY", "dollar_pnl": 200.0, "is_win": True},
    ]
    with patch("app.rh_trade_record.get_all_trades", return_value=trades):
        from app.tax import compute_rh_tax_summary
        result = compute_rh_tax_summary(2025)

    assert result["total_trades"] == 0
    assert result["short_term_net"] == 0.0


# ── Alpaca FIFO matching ──────────────────────────────────────────────────────

def _make_order(symbol, side, qty, price, filled_at):
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.filled_qty = str(qty)
    o.filled_avg_price = str(price)
    o.filled_at = filled_at
    return o


def test_fifo_simple_short_term_gain():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 10, 500.0, datetime(2025, 1, 5, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 10, 550.0, datetime(2025, 6, 10, tzinfo=timezone.utc))

    events = _fifo_match([buy, sell], tax_year=2025)

    assert len(events) == 1
    assert events[0]["short_term"] is True
    assert abs(events[0]["gain"] - 500.0) < 0.01


def test_fifo_simple_long_term_gain():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 5, 400.0, datetime(2024, 1, 1, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 5, 500.0, datetime(2025, 3, 1, tzinfo=timezone.utc))

    events = _fifo_match([buy, sell], tax_year=2025)

    assert len(events) == 1
    assert events[0]["short_term"] is False
    assert abs(events[0]["gain"] - 500.0) < 0.01


def test_fifo_excludes_sells_outside_tax_year():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 10, 500.0, datetime(2024, 1, 5, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 10, 550.0, datetime(2024, 8, 10, tzinfo=timezone.utc))  # 2024

    events = _fifo_match([buy, sell], tax_year=2025)

    assert len(events) == 0


def test_fifo_partial_sell():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 10, 500.0, datetime(2025, 1, 5, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 4, 600.0, datetime(2025, 4, 5, tzinfo=timezone.utc))

    events = _fifo_match([buy, sell], tax_year=2025)

    assert len(events) == 1
    assert abs(events[0]["qty"] - 4.0) < 0.001
    assert abs(events[0]["gain"] - 400.0) < 0.01


def test_fifo_loss():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 10, 600.0, datetime(2025, 2, 1, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 10, 550.0, datetime(2025, 4, 1, tzinfo=timezone.utc))

    events = _fifo_match([buy, sell], tax_year=2025)

    assert events[0]["gain"] < 0
    assert abs(events[0]["gain"] - (-500.0)) < 0.01


def test_fifo_unknown_basis_when_no_buy_lot():
    from app.tax import _fifo_match

    # Sell with no prior buy (position opened before fetch window)
    sell = _make_order("SPY", "sell", 5, 600.0, datetime(2025, 3, 1, tzinfo=timezone.utc))

    events = _fifo_match([sell], tax_year=2025)

    assert len(events) == 1
    assert events[0].get("unknown_basis") is True
    assert events[0]["gain"] is None


# ── /healthz endpoint ─────────────────────────────────────────────────────────

def test_healthz_alpaca_up():
    os.environ.setdefault("DISCORD_APP_ID", "test-app-id")
    from fastapi.testclient import TestClient
    with patch("app.main.get_account", return_value=MagicMock()):
        from app.main import app
        client = TestClient(app)
        resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["alpaca"] == "up"
    assert "robinhood" in data
    assert "timestamp" in data


def test_healthz_alpaca_down():
    os.environ.setdefault("DISCORD_APP_ID", "test-app-id")
    from fastapi.testclient import TestClient
    with patch("app.main.get_account", side_effect=Exception("timeout")):
        from app.main import app
        client = TestClient(app)
        resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["alpaca"] == "down"
    assert "timeout" in data["alpaca_error"]
