import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def test_format_buy_message_contains_ticker_and_action():
    from app.trade_notifier import _format_trade_message
    msg = _format_trade_message(
        ticker="SPY", action="BUY",
        filled_price=537.42, alert_price=537.00,
        filled_qty=5.0, position_qty=5.0,
        dollar_pnl=None, pct_pnl=None,
    )
    assert "BUY" in msg
    assert "SPY" in msg
    assert "🟢" in msg
    assert "537.42" in msg
    assert "5" in msg


def test_format_sell_message_includes_pnl():
    from app.trade_notifier import _format_trade_message
    msg = _format_trade_message(
        ticker="SPY", action="SELL",
        filled_price=551.80, alert_price=551.00,
        filled_qty=5.0, position_qty=0.0,
        dollar_pnl=71.90, pct_pnl=2.68,
    )
    assert "🔴" in msg
    assert "SELL" in msg
    assert "551.80" in msg
    assert "+$71.90" in msg
    assert "+2.68%" in msg
    assert "0" in msg


def test_format_message_uses_alert_price_when_no_fill_price():
    from app.trade_notifier import _format_trade_message
    msg = _format_trade_message(
        ticker="SPY", action="BUY",
        filled_price=None, alert_price=537.00,
        filled_qty=None, position_qty=3.0,
        dollar_pnl=None, pct_pnl=None,
    )
    assert "537.00" in msg
    assert "≈" in msg


def test_format_sell_message_omits_pnl_when_unavailable():
    from app.trade_notifier import _format_trade_message
    msg = _format_trade_message(
        ticker="SPY", action="SELL",
        filled_price=551.80, alert_price=None,
        filled_qty=5.0, position_qty=0.0,
        dollar_pnl=None, pct_pnl=None,
    )
    assert "P&L" not in msg


def test_format_negative_pnl_shows_minus_sign():
    from app.trade_notifier import _format_trade_message
    msg = _format_trade_message(
        ticker="SPY", action="SELL",
        filled_price=520.00, alert_price=None,
        filled_qty=5.0, position_qty=0.0,
        dollar_pnl=-87.50, pct_pnl=-1.65,
    )
    assert "-$87.50" in msg
    assert "-1.65%" in msg


@pytest.mark.asyncio
async def test_notify_trade_calls_notify_trades_for_buy():
    with patch("app.trade_notifier.get_order", return_value=None):
        with patch("app.trade_notifier.get_position", return_value=None):
            with patch("app.trade_notifier.notify_trades", new_callable=AsyncMock) as mock_notify:
                from app.trade_notifier import notify_trade
                await notify_trade(
                    ticker="SPY", action="BUY",
                    result={"orders": []},
                    alert_price=537.00,
                    avg_entry_price=None,
                )
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "BUY" in msg
    assert "SPY" in msg


@pytest.mark.asyncio
async def test_notify_trade_includes_pnl_for_sell():
    fake_order = MagicMock()
    fake_order.filled_avg_price = "551.80"
    fake_order.filled_qty = "5"
    with patch("app.trade_notifier.get_order", return_value=fake_order):
        with patch("app.trade_notifier.get_position", return_value=None):
            with patch("app.trade_notifier.notify_trades", new_callable=AsyncMock) as mock_notify:
                from app.trade_notifier import notify_trade
                await notify_trade(
                    ticker="SPY", action="SELL",
                    result={"orders": [{"alpaca_order_id": "ord-123"}]},
                    alert_price=551.00,
                    avg_entry_price=537.42,
                )
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "P&L" in msg
    assert "SELL" in msg


@pytest.mark.asyncio
async def test_notify_trade_does_not_raise_on_exception():
    with patch("app.trade_notifier.get_order", side_effect=Exception("alpaca down")):
        with patch("app.trade_notifier.get_position", return_value=None):
            with patch("app.trade_notifier.notify_trades", new_callable=AsyncMock):
                from app.trade_notifier import notify_trade
                await notify_trade(
                    ticker="SPY", action="BUY",
                    result={"orders": []},
                    alert_price=537.00,
                    avg_entry_price=None,
                )
