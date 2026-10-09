import asyncio
import json
import os
from types import SimpleNamespace

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

import pytest

from app import leverage_state
from app.trading import order_logic as ol


@pytest.fixture
def broker(tmp_path, monkeypatch):
    """Fake Alpaca: records placed orders, position size is settable."""
    monkeypatch.setattr(leverage_state, "_STATE_FILE", tmp_path / "leverage_entry.json")
    monkeypatch.setattr(ol.settings, "allow_fractional_shares", True)
    state = SimpleNamespace(orders=[], position_qty=None, buying_power=18200.0, closed=[])

    def place_market_order(ticker, side, qty):
        state.orders.append((ticker, side, qty))
        return SimpleNamespace(qty=qty)

    def get_position(ticker):
        return None if state.position_qty is None else SimpleNamespace(qty=str(state.position_qty))

    def close_position(ticker):
        state.closed.append(ticker)
        return SimpleNamespace(qty=state.position_qty)

    monkeypatch.setattr(ol.ac, "place_market_order", place_market_order)
    monkeypatch.setattr(ol.ac, "get_position", get_position)
    monkeypatch.setattr(ol.ac, "close_position", close_position)
    monkeypatch.setattr(ol.ac, "get_account", lambda: SimpleNamespace(buying_power=str(state.buying_power)))
    return state


def test_remove_sells_exactly_what_add_bought(broker):
    # Real numbers from 2026-10-06: bought 2.34, old formula sold 20.35/11 = 1.85.
    ol._kimi_add_leverage("SPY", 779.0, 0.1)
    assert broker.orders == [("SPY", ol.OrderSide.BUY, 2.34)]

    broker.position_qty = 20.35
    ol._kimi_remove_leverage("SPY")

    assert broker.orders[-1] == ("SPY", ol.OrderSide.SELL, 2.34)
    assert leverage_state.load_leverage_qty("SPY") is None


def test_consecutive_adds_are_all_removed(broker):
    ol._kimi_add_leverage("SPY", 779.0, 0.1)
    ol._kimi_add_leverage("SPY", 779.0, 0.1)
    broker.position_qty = 30.0
    ol._kimi_remove_leverage("SPY")
    assert broker.orders[-1] == ("SPY", ol.OrderSide.SELL, 4.68)


def test_remove_without_saved_qty_sells_nothing(broker):
    broker.position_qty = 20.35
    with pytest.raises(ol.LeverageQtyUnknown):
        ol._kimi_remove_leverage("SPY")
    assert broker.orders == []


def test_remove_with_no_position_is_a_noop(broker):
    assert ol._kimi_remove_leverage("SPY") is None
    assert broker.orders == []


def test_remove_never_sells_more_than_the_position(broker):
    ol._kimi_add_leverage("SPY", 779.0, 0.1)
    broker.position_qty = 1.5
    ol._kimi_remove_leverage("SPY")
    assert broker.orders[-1] == ("SPY", ol.OrderSide.SELL, 1.5)
    assert leverage_state.load_leverage_qty("SPY") is None


def test_stop_loss_clears_saved_qty(broker):
    ol._kimi_add_leverage("SPY", 779.0, 0.1)
    broker.position_qty = 20.35
    ol._kimi_stop_loss("SPY")
    assert broker.closed == ["SPY"]
    assert leverage_state.load_leverage_qty("SPY") is None


def test_saving_fill_price_keeps_saved_qty(broker):
    ol._kimi_add_leverage("SPY", 779.0, 0.1)
    # Not asyncio.run(): it unsets the current event loop, which later tests rely on.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(leverage_state.save_leverage_entry("SPY", 779.15))
    finally:
        loop.close()
    assert leverage_state.load_leverage_qty("SPY") == 2.34
    assert leverage_state.load_leverage_entry("SPY") == 779.15
    assert json.loads(leverage_state._STATE_FILE.read_text())["SPY"] == 779.15
