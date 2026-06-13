import os
os.environ.setdefault("ALPACA_API_KEY",    "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET",    "MY_SHARED_SECRET")
os.environ.setdefault("RH_USERNAME",       "test@example.com")
os.environ.setdefault("RH_PASSWORD",       "test_password")
os.environ.setdefault("RH_LEVERAGE_FACTOR","0.5")
os.environ.setdefault("RH_ENABLED",        "true")

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from app.models import TradingAction


# ── login_from_pickle ──────────────────────────────────────────────────────────

def test_login_from_pickle_returns_false_when_no_file():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    with patch("os.path.exists", return_value=False):
        result = client.login_from_pickle()
    assert result is False
    assert client.available is False


def test_login_from_pickle_returns_false_when_rh_disabled():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    with patch("app.trading.robinhood_client.settings") as mock_settings:
        mock_settings.rh_enabled = False
        result = client.login_from_pickle()
    assert result is False
    assert client.available is False


def test_login_from_pickle_sets_available_on_success():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    with patch("os.path.exists", return_value=True), \
         patch("os.makedirs"), \
         patch("shutil.copy2"), \
         patch("robin_stocks.robinhood.login"), \
         patch("robin_stocks.robinhood.load_account_profile", return_value={"cash": "5000.00"}), \
         patch("app.trading.robinhood_client.settings") as mock_settings:
        mock_settings.rh_enabled = True
        mock_settings.rh_username = "test@example.com"
        mock_settings.rh_password = "password"
        result = client.login_from_pickle()
    assert result is True
    assert client.available is True


def test_login_from_pickle_returns_false_on_exception():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    with patch("os.path.exists", return_value=True), \
         patch("os.makedirs"), \
         patch("shutil.copy2"), \
         patch("robin_stocks.robinhood.login", side_effect=Exception("bad token")), \
         patch("app.trading.robinhood_client.settings") as mock_settings:
        mock_settings.rh_enabled = True
        mock_settings.rh_username = "test@example.com"
        mock_settings.rh_password = "password"
        result = client.login_from_pickle()
    assert result is False
    assert client.available is False


# ── login_with_sms ─────────────────────────────────────────────────────────────

def test_login_with_sms_sets_available():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    with patch("robin_stocks.robinhood.login"), \
         patch("os.path.exists", return_value=True), \
         patch("os.makedirs"), \
         patch("shutil.copy2"), \
         patch("app.trading.robinhood_client.settings") as mock_settings:
        mock_settings.rh_username = "test@example.com"
        mock_settings.rh_password = "password"
        client.login_with_sms("123456")
    assert client.available is True


def test_login_with_sms_raises_on_bad_code():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    with patch("robin_stocks.robinhood.login", side_effect=Exception("Invalid MFA")), \
         patch("app.trading.robinhood_client.settings") as mock_settings:
        mock_settings.rh_username = "test@example.com"
        mock_settings.rh_password = "password"
        with pytest.raises(Exception, match="Invalid MFA"):
            client.login_with_sms("000000")
    assert client.available is False


# ── execute() — skipped states ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_skips_when_not_available():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = False
    result = await client.execute(TradingAction.BUY, "SPY")
    assert result["status"] == "skipped"
    assert "session unavailable" in result["reason"]


@pytest.mark.asyncio
async def test_execute_skips_when_rh_disabled():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True
    with patch("app.trading.robinhood_client.settings") as mock_settings:
        mock_settings.rh_enabled = False
        result = await client.execute(TradingAction.BUY, "SPY")
    assert result["status"] == "skipped"


# ── execute() — BUY ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_buy_places_market_order():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("app.trading.robinhood_client.settings") as mock_settings, \
         patch("robin_stocks.robinhood.load_account_profile",
               return_value={"cash": "10000.00"}), \
         patch("robin_stocks.robinhood.get_latest_price",
               return_value=["500.00"]), \
         patch("robin_stocks.robinhood.order_buy_fractional_by_quantity",
               return_value={"id": "rh-order-1"}) as mock_buy:
        mock_settings.rh_enabled = True
        mock_settings.rh_leverage_factor = 0.3
        mock_settings.rh_account_number = None
        result = await client.execute(TradingAction.BUY, "SPY")

    # qty = round(10000 * 0.3 / 500, 6) = 6.0
    mock_buy.assert_called_once_with("SPY", 6.0, account_number=None)
    assert result["status"] == "ok"
    assert result["side"] == "buy"
    assert result["qty"] == 6.0


# ── execute() — order classification (queued vs. filled) ─────────────────────
#
# robin_stocks always returns state="unconfirmed" on submission, whether or not
# the market is open, so that field can't tell us if the order will fill now or
# queue for the next session. The classification must come from Alpaca's market
# clock instead — these tests pin down both branches of that decision.

@pytest.mark.asyncio
async def test_execute_buy_reports_immediate_fill_when_market_open():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("app.trading.robinhood_client.settings") as mock_settings, \
         patch("app.trading.robinhood_client.ac.is_market_open", return_value=True), \
         patch("robin_stocks.robinhood.load_account_profile",
               return_value={"cash": "10000.00"}), \
         patch("robin_stocks.robinhood.get_latest_price",
               return_value=["500.00"]), \
         patch("robin_stocks.robinhood.get_open_stock_positions",
               return_value=[{"instrument": "url", "quantity": "6.0"}]), \
         patch("robin_stocks.robinhood.get_instrument_by_url",
               return_value={"symbol": "SPY"}), \
         patch("robin_stocks.robinhood.order_buy_fractional_by_quantity",
               return_value={"id": "rh-order-1", "state": "unconfirmed", "average_price": "501.00"}):
        mock_settings.rh_enabled = True
        mock_settings.rh_leverage_factor = 0.3
        mock_settings.rh_account_number = None
        result = await client.execute(TradingAction.BUY, "SPY")

    assert result["status"] == "ok"
    assert "queued" not in result
    assert result["fill_price"] == 501.00


@pytest.mark.asyncio
async def test_execute_buy_reports_queued_when_market_closed():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("app.trading.robinhood_client.settings") as mock_settings, \
         patch("app.trading.robinhood_client.ac.is_market_open", return_value=False), \
         patch("robin_stocks.robinhood.load_account_profile",
               return_value={"cash": "10000.00"}), \
         patch("robin_stocks.robinhood.get_latest_price",
               return_value=["500.00"]), \
         patch("robin_stocks.robinhood.order_buy_fractional_by_quantity",
               return_value={"id": "rh-order-1", "state": "unconfirmed"}):
        mock_settings.rh_enabled = True
        mock_settings.rh_leverage_factor = 0.3
        mock_settings.rh_account_number = None
        result = await client.execute(TradingAction.BUY, "SPY")

    assert result["status"] == "ok"
    assert result["queued"] is True
    assert result["price_est"] == 500.00
    assert "fill_price" not in result


# ── execute() — CLOSE_LONG / SELL / STOP_LOSS ─────────────────────────────────

@pytest.mark.asyncio
async def test_execute_close_long_sells_full_position():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    mock_position = {
        "instrument": "https://rh.com/instruments/123/",
        "quantity": "5.0000",
        "average_buy_price": "490.00",
    }
    mock_instrument = {"symbol": "SPY"}

    with patch("app.trading.robinhood_client.settings") as mock_settings, \
         patch("robin_stocks.robinhood.get_open_stock_positions",
               return_value=[mock_position]), \
         patch("robin_stocks.robinhood.get_instrument_by_url",
               return_value=mock_instrument), \
         patch("robin_stocks.robinhood.get_latest_price",
               return_value=["500.00"]), \
         patch("robin_stocks.robinhood.order_sell_fractional_by_quantity",
               return_value={"id": "rh-close-1"}) as mock_sell:
        mock_settings.rh_enabled = True
        mock_settings.rh_account_number = None
        result = await client.execute(TradingAction.CLOSE_LONG, "SPY")

    mock_sell.assert_called_once_with("SPY", 5.0, account_number=None)
    assert result["status"] == "ok"
    assert result["side"] == "sell"
    assert result["avg_buy_price"] == 490.0
    assert result["qty"] == 5.0


@pytest.mark.asyncio
async def test_execute_close_long_no_position_returns_ok():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("app.trading.robinhood_client.settings") as mock_settings, \
         patch("robin_stocks.robinhood.get_open_stock_positions", return_value=[]):
        mock_settings.rh_enabled = True
        result = await client.execute(TradingAction.CLOSE_LONG, "SPY")

    assert result["status"] == "ok"
    assert "no position" in result["note"]


# ── execute() — CLOSE_SHORT / REVERSE_TO_SHORT ────────────────────────────────

@pytest.mark.asyncio
async def test_execute_close_short_is_noop():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("app.trading.robinhood_client.settings") as mock_settings, \
         patch("robin_stocks.robinhood.get_open_stock_positions", return_value=[]):
        mock_settings.rh_enabled = True
        result = await client.execute(TradingAction.CLOSE_SHORT, "SPY")

    assert result["status"] == "ok"
    assert "short not supported" in result["note"]


# ── execute() — auth error marks unavailable ──────────────────────────────────

@pytest.mark.asyncio
async def test_execute_marks_unavailable_on_auth_error():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("app.trading.robinhood_client.settings") as mock_settings, \
         patch("robin_stocks.robinhood.load_account_profile",
               side_effect=Exception("401 unauthorized")):
        mock_settings.rh_enabled = True
        mock_settings.rh_leverage_factor = 0.3
        result = await client.execute(TradingAction.BUY, "SPY")

    assert result["status"] == "failed"
    assert result["reason"] == "session expired"
    assert client.available is False


# ── execute() — BASE_ENTRY is skipped ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_base_entry_is_skipped():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("app.trading.robinhood_client.settings") as mock_settings:
        mock_settings.rh_enabled = True
        result = await client.execute(TradingAction.BASE_ENTRY, "SPY")

    assert result["status"] == "skipped"


# ── get_equity_history_async ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_equity_history_returns_none_when_unavailable():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = False

    result = await client.get_equity_history_async("day", "5minute")

    assert result is None


@pytest.mark.asyncio
async def test_get_equity_history_returns_equity_and_timestamps():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    historicals = {
        "equity_historicals": [
            {"begins_at": "2026-06-12T09:30:00Z", "adjusted_close_equity": "10050.00"},
            {"begins_at": "2026-06-12T15:30:00Z", "adjusted_close_equity": "10100.00"},
        ]
    }
    with patch("robin_stocks.robinhood.get_historical_portfolio", return_value=historicals):
        result = await client.get_equity_history_async("day", "5minute")

    assert result is not None
    equity, timestamps = result
    assert equity == [10050.00, 10100.00]
    assert len(timestamps) == 2
    assert all(isinstance(t, int) for t in timestamps)


@pytest.mark.asyncio
async def test_get_equity_history_falls_back_to_unadjusted_close_equity():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    historicals = {
        "equity_historicals": [
            {"begins_at": "2026-06-12T09:30:00Z", "close_equity": "5000.00"},
        ]
    }
    with patch("robin_stocks.robinhood.get_historical_portfolio", return_value=historicals):
        result = await client.get_equity_history_async("day", "5minute")

    assert result is not None
    equity, _ = result
    assert equity == [5000.00]


@pytest.mark.asyncio
async def test_get_equity_history_falls_back_to_open_equity_when_close_is_null():
    """Some span/interval combos (e.g. weekly/hourly) leave close fields null
    on every bar — fall back to open equity rather than returning None."""
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    historicals = {
        "equity_historicals": [
            {"begins_at": "2026-06-08T09:30:00Z", "adjusted_open_equity": "10000.00", "adjusted_close_equity": None, "close_equity": None},
            {"begins_at": "2026-06-12T15:30:00Z", "adjusted_open_equity": "10100.00", "adjusted_close_equity": None, "close_equity": None},
        ]
    }
    with patch("robin_stocks.robinhood.get_historical_portfolio", return_value=historicals):
        result = await client.get_equity_history_async("week", "hour")

    assert result is not None
    equity, timestamps = result
    assert equity == [10000.00, 10100.00]
    assert len(timestamps) == 2


@pytest.mark.asyncio
async def test_get_equity_history_returns_none_on_empty_historicals():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("robin_stocks.robinhood.get_historical_portfolio", return_value={"equity_historicals": []}):
        result = await client.get_equity_history_async("day", "5minute")

    assert result is None


@pytest.mark.asyncio
async def test_get_equity_history_returns_none_on_exception():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    with patch("robin_stocks.robinhood.get_historical_portfolio", side_effect=Exception("network error")):
        result = await client.get_equity_history_async("day", "5minute")

    assert result is None


@pytest.mark.asyncio
async def test_get_equity_history_ytd_filters_by_since():
    from app.trading.robinhood_client import RobinhoodClient
    from datetime import datetime, timezone
    client = RobinhoodClient()
    client.available = True

    historicals = {
        "equity_historicals": [
            {"begins_at": "2025-12-15T00:00:00Z", "adjusted_close_equity": "8500.00"},
            {"begins_at": "2026-01-05T00:00:00Z", "adjusted_close_equity": "9090.00"},
            {"begins_at": "2026-06-01T00:00:00Z", "adjusted_close_equity": "9690.00"},
        ]
    }
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with patch("robin_stocks.robinhood.get_historical_portfolio", return_value=historicals):
        result = await client.get_equity_history_async("year", "day", since=since)

    assert result is not None
    equity, _ = result
    assert equity == [9090.00, 9690.00]
