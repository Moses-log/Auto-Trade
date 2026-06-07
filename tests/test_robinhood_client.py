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
