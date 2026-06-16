import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET", "test_secret")

from app.public_stats import compute_stats


def _order(side, qty, price, dt_str):
    o = MagicMock()
    o.side = MagicMock()
    o.side.__str__ = lambda self: f"OrderSide.{side.upper()}"
    o.filled_qty = str(qty)
    o.filled_avg_price = str(price)
    o.filled_at = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
    return o


def test_empty_orders_returns_zero_stats():
    result = compute_stats([])
    assert result["trades"] == 0
    assert result["wins"] == 0
    assert result["win_rate"] == 0


def test_single_win_round_trip():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T10:00:00"),
        _order("SELL", 10, 110.0, "2026-04-01T11:00:00"),
    ]
    result = compute_stats(orders)
    assert result["trades"] == 1
    assert result["wins"] == 1
    assert result["losses"] == 0
    assert result["win_rate"] == 100.0
    assert result["profit_factor"] == 0  # no losses to divide by


def test_single_loss_round_trip():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T10:00:00"),
        _order("SELL", 10,  90.0, "2026-04-01T11:00:00"),
    ]
    result = compute_stats(orders)
    assert result["trades"] == 1
    assert result["losses"] == 1
    assert result["win_rate"] == 0.0


def test_lifo_matching_uses_most_recent_buy():
    # Base buy at 100, then leverage buy at 120, then sell at 125.
    # LIFO should match sell against the 120 buy (profit), not the 100 buy.
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T09:00:00"),  # base (never matched)
        _order("BUY",  10, 120.0, "2026-04-01T10:00:00"),  # leverage
        _order("SELL", 10, 125.0, "2026-04-01T11:00:00"),  # removes leverage
    ]
    result = compute_stats(orders)
    assert result["trades"] == 1
    assert result["wins"] == 1
    dollar_pnl = result["cumulative_returns"][0]["pct"]
    assert dollar_pnl > 0  # sold higher than the 120 buy


def test_profit_factor_calculation():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T10:00:00"),
        _order("SELL", 10, 110.0, "2026-04-01T11:00:00"),  # +$100 win
        _order("BUY",  10, 100.0, "2026-04-02T10:00:00"),
        _order("SELL", 10,  90.0, "2026-04-02T11:00:00"),  # -$100 loss
    ]
    result = compute_stats(orders)
    assert result["profit_factor"] == 1.0


def test_cumulative_returns_length_matches_trades():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T10:00:00"),
        _order("SELL", 10, 110.0, "2026-04-01T11:00:00"),
        _order("BUY",  10, 100.0, "2026-04-02T10:00:00"),
        _order("SELL", 10, 105.0, "2026-04-02T11:00:00"),
    ]
    result = compute_stats(orders)
    assert len(result["cumulative_returns"]) == 2
    assert result["cumulative_returns"][0]["trade"] == 1
    assert result["cumulative_returns"][1]["trade"] == 2


def test_date_range_uses_first_buy_and_last_sell():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-22T10:00:00"),
        _order("SELL", 10, 110.0, "2026-06-11T11:00:00"),
    ]
    result = compute_stats(orders)
    assert "Apr" in result["date_range"]["from"]
    assert "Jun" in result["date_range"]["to"]
