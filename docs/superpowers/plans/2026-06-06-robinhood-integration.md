# Robinhood Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror every TradingView trade executed on Alpaca to Robinhood in parallel, with SMS-based re-authentication via a `/robinhood-auth` endpoint.

**Architecture:** A new `RobinhoodClient` singleton (module-level, same pattern as `alpaca_client`) wraps the synchronous `robin_stocks` library in `asyncio.run_in_executor`. `execute_action()` awaits `rh_client.execute()` after Alpaca logic completes and attaches the result to the response dict. `main.py` reads that result to post Robinhood-specific Discord notifications.

**Tech Stack:** `robin_stocks>=2.1.0` (unofficial Robinhood Python API), `asyncio.run_in_executor` for thread-pool offload, `/data/robinhood.pickle` on Render persistent disk for session persistence.

---

## File Map

| File | Action | What changes |
|---|---|---|
| `requirements.txt` | Modify | Add `robin-stocks>=2.1.0` |
| `app/config.py` | Modify | Add `rh_username`, `rh_password`, `rh_leverage_factor`, `rh_enabled`, `rh_discord_webhook_url` |
| `app/notifications.py` | Modify | Add `notify_robinhood()` posting to `RH_DISCORD_WEBHOOK_URL` |
| `app/trading/robinhood_client.py` | Create | `RobinhoodClient` class + `rh_client` singleton |
| `app/main.py` | Modify | Startup pickle login, `/robinhood-auth` endpoint, Robinhood Discord notifications |
| `app/trading/order_logic.py` | Modify | Call `rh_client.execute()` after Alpaca, add `result["robinhood"]` |
| `.env.example` | Modify | Document new `RH_*` env vars |
| `tests/test_robinhood_client.py` | Create | Unit tests for `RobinhoodClient` |
| `tests/test_webhook.py` | Modify | Verify `result["robinhood"]` is present; add `/robinhood-auth` tests |

---

## Task 1: Add robin-stocks to requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add the dependency**

Open `requirements.txt` and add this line after the `alpaca-py` line:

```
# Robinhood trading (unofficial API)
robin-stocks>=2.1.0
```

- [ ] **Step 2: Install it**

```bash
pip install robin-stocks>=2.1.0
```

Expected: installs successfully with no conflicts.

- [ ] **Step 3: Verify import works**

```bash
python -c "import robin_stocks.robinhood as r; print('robin_stocks OK')"
```

Expected: prints `robin_stocks OK`.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "feat: add robin-stocks dependency"
```

---

## Task 2: Add Robinhood settings to config.py

**Files:**
- Modify: `app/config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_rh.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY",    "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET",    "MY_SHARED_SECRET")

from app.config import settings

def test_rh_defaults():
    assert settings.rh_enabled is True
    assert settings.rh_leverage_factor == 0.3
    assert settings.rh_username is None
    assert settings.rh_password is None
    assert settings.rh_discord_webhook_url is None
```

- [ ] **Step 2: Run to confirm it fails**

```bash
pytest tests/test_config_rh.py -v
```

Expected: `AttributeError: 'Settings' object has no attribute 'rh_enabled'`

- [ ] **Step 3: Add settings to config.py**

In `app/config.py`, add after the `allow_fractional_shares` line:

```python
    # ── Robinhood ─────────────────────────────────────────────────────────────
    rh_username: Optional[str] = None
    rh_password: Optional[str] = None
    rh_leverage_factor: float = 0.3
    rh_enabled: bool = True
    rh_discord_webhook_url: Optional[str] = None
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
pytest tests/test_config_rh.py -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config_rh.py
git commit -m "feat: add Robinhood config settings"
```

---

## Task 3: Add notify_robinhood() to notifications.py

**Files:**
- Modify: `app/notifications.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notifications_rh.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY",    "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET",    "MY_SHARED_SECRET")

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

@pytest.mark.asyncio
async def test_notify_robinhood_uses_rh_url():
    """Posts to RH_DISCORD_WEBHOOK_URL when set."""
    with patch("app.notifications.settings") as mock_settings, \
         patch("httpx.AsyncClient") as mock_client_class:
        mock_settings.rh_discord_webhook_url = "https://discord.com/rh-channel"
        mock_settings.discord_webhook_url = "https://discord.com/main-channel"

        mock_response = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_class.return_value = mock_client

        from app.notifications import notify_robinhood
        await notify_robinhood("test message")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://discord.com/rh-channel"


@pytest.mark.asyncio
async def test_notify_robinhood_falls_back_to_main_url():
    """Falls back to main DISCORD_WEBHOOK_URL when RH url is not set."""
    with patch("app.notifications.settings") as mock_settings, \
         patch("httpx.AsyncClient") as mock_client_class:
        mock_settings.rh_discord_webhook_url = None
        mock_settings.discord_webhook_url = "https://discord.com/main-channel"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_client_class.return_value = mock_client

        from app.notifications import notify_robinhood
        await notify_robinhood("test message")

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://discord.com/main-channel"


@pytest.mark.asyncio
async def test_notify_robinhood_silent_when_no_urls():
    """Does nothing (no-op) when neither URL is set."""
    with patch("app.notifications.settings") as mock_settings, \
         patch("httpx.AsyncClient") as mock_client_class:
        mock_settings.rh_discord_webhook_url = None
        mock_settings.discord_webhook_url = None

        from app.notifications import notify_robinhood
        await notify_robinhood("test message")  # should not raise

        mock_client_class.assert_not_called()
```

- [ ] **Step 2: Run to confirm it fails**

```bash
pytest tests/test_notifications_rh.py -v
```

Expected: `ImportError: cannot import name 'notify_robinhood'`

- [ ] **Step 3: Add notify_robinhood() to notifications.py**

At the end of `app/notifications.py`, add:

```python
async def notify_robinhood(message: str) -> None:
    """Send a notification to the Robinhood Discord channel.
    Falls back to the main Discord channel if RH_DISCORD_WEBHOOK_URL is not set.
    """
    url = settings.rh_discord_webhook_url or settings.discord_webhook_url
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"content": message[:2000]})
    except Exception as exc:
        log.warning("Robinhood Discord notification failed: %s", exc)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_notifications_rh.py -v
```

Expected: all 3 `PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/notifications.py tests/test_notifications_rh.py
git commit -m "feat: add notify_robinhood() for separate Discord channel"
```

---

## Task 4: Create app/trading/robinhood_client.py

**Files:**
- Create: `app/trading/robinhood_client.py`
- Create: `tests/test_robinhood_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_robinhood_client.py`:

```python
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
         patch("robin_stocks.robinhood.order_buy_market",
               return_value={"id": "rh-order-1"}) as mock_buy:
        mock_settings.rh_enabled = True
        mock_settings.rh_leverage_factor = 0.3
        result = await client.execute(TradingAction.BUY, "SPY")

    # qty = floor(10000 * 0.3 / 500) = floor(6.0) = 6
    mock_buy.assert_called_once_with("SPY", 6)
    assert result["status"] == "ok"
    assert result["side"] == "buy"
    assert result["qty"] == 6


# ── execute() — CLOSE_LONG / SELL / STOP_LOSS ─────────────────────────────────

@pytest.mark.asyncio
async def test_execute_close_long_sells_full_position():
    from app.trading.robinhood_client import RobinhoodClient
    client = RobinhoodClient()
    client.available = True

    mock_position = {"instrument": "https://rh.com/instruments/123/", "quantity": "5.0000"}
    mock_instrument = {"symbol": "SPY"}

    with patch("app.trading.robinhood_client.settings") as mock_settings, \
         patch("robin_stocks.robinhood.get_open_stock_positions",
               return_value=[mock_position]), \
         patch("robin_stocks.robinhood.get_instrument_by_url",
               return_value=mock_instrument), \
         patch("robin_stocks.robinhood.order_sell_market",
               return_value={"id": "rh-close-1"}) as mock_sell:
        mock_settings.rh_enabled = True
        result = await client.execute(TradingAction.CLOSE_LONG, "SPY")

    mock_sell.assert_called_once_with("SPY", 5)
    assert result["status"] == "ok"
    assert result["side"] == "sell"


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
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
pytest tests/test_robinhood_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.trading.robinhood_client'`

- [ ] **Step 3: Create app/trading/robinhood_client.py**

```python
"""
robinhood_client.py — Robinhood authentication and order execution.

Uses robin_stocks (unofficial Robinhood API). Since robin_stocks is
synchronous, all trade calls run via asyncio.run_in_executor so they
don't block FastAPI's event loop.

Session token is stored at /data/robinhood.pickle on Render's persistent
disk. On startup, login_from_pickle() restores the session automatically.
When the token expires, login_with_sms() re-authenticates using an SMS
code and saves a fresh token.
"""

import asyncio
import logging
import math
import os
import shutil
from typing import Optional

import robin_stocks.robinhood as r

from app.config import settings

log = logging.getLogger(__name__)

_PICKLE_BACKUP = "/data/robinhood.pickle"
_TOKENS_DIR    = os.path.expanduser("~/.tokens")
_PICKLE_PATH   = os.path.join(_TOKENS_DIR, "robinhood.pickle")


class RobinhoodClient:
    def __init__(self):
        self.available: bool = False

    # ── Auth ────────────────────────────────────────────────────────────────────

    def login_from_pickle(self) -> bool:
        """Restore session from /data/robinhood.pickle. Returns True if valid."""
        if not settings.rh_enabled:
            return False
        if not os.path.exists(_PICKLE_BACKUP):
            log.warning("Robinhood pickle not found at %s", _PICKLE_BACKUP)
            return False
        os.makedirs(_TOKENS_DIR, exist_ok=True)
        shutil.copy2(_PICKLE_BACKUP, _PICKLE_PATH)
        try:
            r.login(
                username=settings.rh_username,
                password=settings.rh_password,
                store_session=True,
            )
            r.load_account_profile()  # verify token is actually valid
            self.available = True
            log.info("Robinhood session restored from pickle")
            return True
        except Exception as exc:
            log.warning("Robinhood pickle login failed: %s", exc)
            self.available = False
            return False

    def login_with_sms(self, sms_code: str) -> None:
        """Authenticate with SMS 2FA code. Saves new session to disk. Raises on failure."""
        r.login(
            username=settings.rh_username,
            password=settings.rh_password,
            mfa_code=sms_code,
            store_session=True,
        )
        os.makedirs(_TOKENS_DIR, exist_ok=True)
        if os.path.exists(_PICKLE_PATH):
            os.makedirs(os.path.dirname(_PICKLE_BACKUP) or "/data", exist_ok=True)
            shutil.copy2(_PICKLE_PATH, _PICKLE_BACKUP)
        self.available = True
        log.info("Robinhood authenticated via SMS code")

    # ── Trade execution ─────────────────────────────────────────────────────────

    async def execute(self, action, ticker: str) -> dict:
        """
        Execute a trade action on Robinhood. Never raises.
        Returns a status dict: {"status": "ok"|"failed"|"skipped", ...}
        """
        from app.models import TradingAction

        if not settings.rh_enabled:
            return {"status": "skipped", "reason": "RH_ENABLED=false"}
        if not self.available:
            return {"status": "skipped", "reason": "session unavailable"}

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._execute_sync, action, ticker)
        except Exception as exc:
            if _is_auth_error(exc):
                self.available = False
                log.error("Robinhood auth error — marking unavailable: %s", exc)
                return {"status": "failed", "reason": "session expired"}
            log.error("Robinhood trade error %s %s: %s", action, ticker, exc)
            return {"status": "failed", "reason": str(exc)}

    def _execute_sync(self, action, ticker: str) -> dict:
        from app.models import TradingAction

        if action in (TradingAction.BUY, TradingAction.ADD_LEVERAGE):
            qty = self._calculate_buy_qty(ticker)
            order = self._place_market_buy(ticker, qty)
            return {"status": "ok", "side": "buy", "qty": qty, "order_id": order.get("id")}

        elif action == TradingAction.REVERSE_TO_LONG:
            self._close_position(ticker)  # close short if any (no-op on standard accounts)
            qty = self._calculate_buy_qty(ticker)
            order = self._place_market_buy(ticker, qty)
            return {"status": "ok", "side": "buy", "qty": qty, "order_id": order.get("id")}

        elif action in (TradingAction.SELL, TradingAction.CLOSE_LONG,
                        TradingAction.REMOVE_LEVERAGE, TradingAction.STOP_LOSS):
            order = self._close_position(ticker)
            if order is None:
                return {"status": "ok", "note": "no position to close"}
            return {"status": "ok", "side": "sell", "order_id": order.get("id")}

        elif action in (TradingAction.CLOSE_SHORT, TradingAction.REVERSE_TO_SHORT):
            # Shorting not supported on standard Robinhood accounts.
            # Close any long position if present; skip the short open.
            order = self._close_position(ticker)
            return {
                "status": "ok",
                "note": "short not supported — closed long if present",
                "closed_long": order is not None,
            }

        elif action == TradingAction.BASE_ENTRY:
            return {"status": "skipped", "reason": "base_entry intentionally skipped"}

        return {"status": "skipped", "reason": f"unknown action: {action}"}

    # ── Private helpers ─────────────────────────────────────────────────────────

    def _calculate_buy_qty(self, ticker: str) -> int:
        buying_power = self._get_buying_power()
        price        = self._get_latest_price(ticker)
        qty          = math.floor((buying_power * settings.rh_leverage_factor) / price)
        if qty <= 0:
            raise ValueError(
                f"Robinhood buy qty is 0 — buying_power={buying_power}, "
                f"price={price}, rh_leverage_factor={settings.rh_leverage_factor}"
            )
        return qty

    def _get_buying_power(self) -> float:
        profile = r.load_account_profile()
        bp = profile.get("cash") or profile.get("portfolio_cash") or "0"
        return float(bp)

    def _get_latest_price(self, ticker: str) -> float:
        prices = r.get_latest_price(ticker)
        if not prices or prices[0] is None:
            raise ValueError(f"Could not get price for {ticker}")
        return float(prices[0])

    def _place_market_buy(self, ticker: str, qty: int) -> dict:
        result = r.order_buy_market(ticker, qty)
        log.info("Robinhood BUY placed", extra={"ticker": ticker, "qty": qty})
        return result or {}

    def _place_market_sell(self, ticker: str, qty: int) -> dict:
        result = r.order_sell_market(ticker, qty)
        log.info("Robinhood SELL placed", extra={"ticker": ticker, "qty": qty})
        return result or {}

    def _get_position(self, ticker: str) -> Optional[dict]:
        positions = r.get_open_stock_positions()
        for pos in (positions or []):
            instrument = r.get_instrument_by_url(pos["instrument"])
            if instrument.get("symbol", "").upper() == ticker.upper():
                return pos
        return None

    def _close_position(self, ticker: str) -> Optional[dict]:
        pos = self._get_position(ticker)
        if pos is None:
            return None
        qty = math.floor(float(pos.get("quantity", 0)))
        if qty <= 0:
            return None
        return self._place_market_sell(ticker, qty)


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("token", "unauthorized", "401", "login", "unauthenticated"))


# Module-level singleton — import this in order_logic.py and main.py
rh_client = RobinhoodClient()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_robinhood_client.py -v
```

Expected: all tests `PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/trading/robinhood_client.py tests/test_robinhood_client.py
git commit -m "feat: add RobinhoodClient with session management and order execution"
```

---

## Task 5: Add /robinhood-auth endpoint and startup login to main.py

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_webhook.py`, at the end of the file:

```python
# ── /robinhood-auth ───────────────────────────────────────────────────────────

@patch("app.main.rh_client")
def test_robinhood_auth_wrong_secret(mock_rh_client):
    r = client.post("/robinhood-auth", json={"secret": "WRONG", "sms_code": "123456"})
    assert r.status_code == 401


@patch("app.main.rh_client")
def test_robinhood_auth_bad_sms_code(mock_rh_client):
    mock_rh_client.login_with_sms.side_effect = Exception("Invalid MFA code")
    r = client.post("/robinhood-auth", json={"secret": "MY_SHARED_SECRET", "sms_code": "000000"})
    assert r.status_code == 400
    assert "Invalid" in r.json()["detail"]


@patch("app.main.rh_client")
def test_robinhood_auth_success(mock_rh_client):
    mock_rh_client.login_with_sms.return_value = None
    r = client.post("/robinhood-auth", json={"secret": "MY_SHARED_SECRET", "sms_code": "123456"})
    assert r.status_code == 200
    assert r.json()["status"] == "authenticated"
    mock_rh_client.login_with_sms.assert_called_once_with("123456")
```

- [ ] **Step 2: Run to confirm they fail**

```bash
pytest tests/test_webhook.py::test_robinhood_auth_wrong_secret tests/test_webhook.py::test_robinhood_auth_bad_sms_code tests/test_webhook.py::test_robinhood_auth_success -v
```

Expected: `404 Not Found` (endpoint doesn't exist yet)

- [ ] **Step 3: Update main.py**

At the top of `app/main.py`, add these imports alongside the existing ones:

```python
from pydantic import BaseModel
from app.notifications import notify, notify_robinhood
from app.trading.robinhood_client import rh_client
```

Replace the existing `from app.notifications import notify` line with the line above.

Replace the existing lifespan function:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "TradingView → Alpaca webhook server starting",
        extra={"paper_trading": "paper" in settings.alpaca_base_url},
    )
    if settings.rh_enabled:
        if not rh_client.login_from_pickle():
            await notify_robinhood(
                "⚠️ Robinhood session unavailable — POST /robinhood-auth "
                "with your SMS code to activate."
            )
    setup_jobs()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    log.info("Server shutting down.")
```

Add this Pydantic model just before the routes section (after the exception handlers):

```python
class _RobinhoodAuthRequest(BaseModel):
    secret: str
    sms_code: str
```

Add this endpoint after the `/health` endpoint:

```python
@app.post("/robinhood-auth", tags=["trading"])
async def robinhood_auth(body: _RobinhoodAuthRequest):
    """Re-authenticate Robinhood session using an SMS 2FA code."""
    try:
        verify_webhook_secret(body.secret)
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "Unauthorized."},
        )
    try:
        rh_client.login_with_sms(body.sms_code)
        await notify_robinhood("Robinhood session restored ✅")
        log.info("Robinhood session re-authenticated successfully")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "authenticated"},
        )
    except Exception as exc:
        log.warning("Robinhood re-auth failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Invalid SMS code or credentials."},
        )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_webhook.py -v
```

Expected: all existing tests still pass + the 3 new auth tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_webhook.py
git commit -m "feat: add /robinhood-auth endpoint and startup session restore"
```

---

## Task 6: Call rh_client.execute() in order_logic.py

**Files:**
- Modify: `app/trading/order_logic.py`

- [ ] **Step 1: Write the failing test**

Add this to `tests/test_webhook.py`:

```python
# ── Robinhood parallel execution ──────────────────────────────────────────────

@patch("app.trading.order_logic.rh_client")
@patch("app.trading.alpaca_client.get_client")
def test_webhook_result_includes_robinhood_key(mock_get_client, mock_rh_client):
    from unittest.mock import AsyncMock
    mock_client = MagicMock()
    mock_client.submit_order.return_value = _mock_order(side="buy")
    mock_get_client.return_value = mock_client

    mock_rh_client.execute = AsyncMock(
        return_value={"status": "ok", "side": "buy", "qty": 3}
    )

    payload = _load_sample("buy")
    payload["order_id"] = "rh_parallel_test_001"
    r = client.post("/webhook", json=payload)

    assert r.status_code == 200
    result = r.json()["result"]
    assert "robinhood" in result
    assert result["robinhood"]["status"] == "ok"
```

- [ ] **Step 2: Run to confirm it fails**

```bash
pytest tests/test_webhook.py::test_webhook_result_includes_robinhood_key -v
```

Expected: `AssertionError: assert 'robinhood' in {...}` (key not present yet)

- [ ] **Step 3: Update order_logic.py**

Add these imports to the top of `app/trading/order_logic.py` (after the existing imports):

```python
from app.trading.robinhood_client import rh_client
```

In the `execute_action()` function, add these two lines immediately before `return result` (the very last line of the function):

```python
    rh_result = await rh_client.execute(action, ticker)
    result["robinhood"] = rh_result

    return result
```

That's the complete change — `rh_client.execute()` is called after all Alpaca logic, and the result is attached to the response.

- [ ] **Step 4: Add Robinhood Discord notification in main.py**

In `app/main.py`, in the `/webhook` handler, update the trade execution block. Replace:

```python
        await notify(
            f"✅ <b>{payload.action.upper()}</b> {payload.ticker} "
            f"| qty={payload.contracts} | price≈{payload.price}"
        )
```

With:

```python
        await notify(
            f"✅ <b>{payload.action.upper()}</b> {payload.ticker} "
            f"| qty={payload.contracts} | price≈{payload.price}"
        )

        rh = result.get("robinhood", {})
        if rh.get("status") == "ok":
            qty_info = f" — {rh['qty']} shares" if rh.get("qty") else ""
            await notify_robinhood(
                f"✅ {payload.action.upper()} {payload.ticker}{qty_info}"
            )
        elif rh.get("status") == "failed":
            reason = rh.get("reason", "unknown")
            await notify_robinhood(
                f"❌ Robinhood {payload.action.upper()} {payload.ticker} FAILED: {reason}"
            )
            if reason == "session expired":
                await notify_robinhood(
                    "⚠️ Robinhood session expired — POST /robinhood-auth to re-authenticate"
                )
```

- [ ] **Step 5: Run all tests**

```bash
pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/trading/order_logic.py app/main.py tests/test_webhook.py
git commit -m "feat: mirror TradingView alerts to Robinhood in parallel with Alpaca"
```

---

## Task 7: Update .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add Robinhood section to .env.example**

Open `.env.example` and add this section at the end:

```env
# ── Robinhood ─────────────────────────────────────────────────────────────────
# Set to false to disable Robinhood entirely without removing config.
RH_ENABLED=true

# Your Robinhood login credentials.
RH_USERNAME=your@email.com
RH_PASSWORD=yourpassword

# Fraction of Robinhood buying power to use per trade.
# Example: 0.3 means 30% of available cash per BUY/ADD_LEVERAGE signal.
RH_LEVERAGE_FACTOR=0.3

# Separate Discord channel for Robinhood trade alerts.
# Falls back to DISCORD_WEBHOOK_URL if not set.
RH_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# ── Robinhood session setup ────────────────────────────────────────────────────
# On first deploy: leave RH_ENABLED=true and deploy. The service will send a
# Discord alert asking you to POST /robinhood-auth with your SMS code.
# After that, the session token is saved to /data/robinhood.pickle and
# auto-refreshes without further SMS codes for several weeks.
#
# To re-authenticate after session expiry:
#   curl -X POST https://your-render-url/robinhood-auth \
#        -H "Content-Type: application/json" \
#        -d '{"secret":"YOUR_WEBHOOK_SECRET","sms_code":"123456"}'
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: add Robinhood env vars to .env.example"
```

---

## Task 8: Run full test suite and verify

- [ ] **Step 1: Run all tests**

```bash
pytest tests/ -v
```

Expected output includes all of these passing:
```
tests/test_config_rh.py::test_rh_defaults PASSED
tests/test_notifications_rh.py::test_notify_robinhood_uses_rh_url PASSED
tests/test_notifications_rh.py::test_notify_robinhood_falls_back_to_main_url PASSED
tests/test_notifications_rh.py::test_notify_robinhood_silent_when_no_urls PASSED
tests/test_robinhood_client.py::test_login_from_pickle_returns_false_when_no_file PASSED
tests/test_robinhood_client.py::test_login_from_pickle_returns_false_when_rh_disabled PASSED
tests/test_robinhood_client.py::test_login_from_pickle_sets_available_on_success PASSED
tests/test_robinhood_client.py::test_login_from_pickle_returns_false_on_exception PASSED
tests/test_robinhood_client.py::test_login_with_sms_sets_available PASSED
tests/test_robinhood_client.py::test_login_with_sms_raises_on_bad_code PASSED
tests/test_robinhood_client.py::test_execute_skips_when_not_available PASSED
tests/test_robinhood_client.py::test_execute_skips_when_rh_disabled PASSED
tests/test_robinhood_client.py::test_execute_buy_places_market_order PASSED
tests/test_robinhood_client.py::test_execute_close_long_sells_full_position PASSED
tests/test_robinhood_client.py::test_execute_close_long_no_position_returns_ok PASSED
tests/test_robinhood_client.py::test_execute_close_short_is_noop PASSED
tests/test_robinhood_client.py::test_execute_marks_unavailable_on_auth_error PASSED
tests/test_robinhood_client.py::test_execute_base_entry_is_skipped PASSED
tests/test_webhook.py::test_robinhood_auth_wrong_secret PASSED
tests/test_webhook.py::test_robinhood_auth_bad_sms_code PASSED
tests/test_webhook.py::test_robinhood_auth_success PASSED
tests/test_webhook.py::test_webhook_result_includes_robinhood_key PASSED
```

- [ ] **Step 2: Final commit if any loose files remain**

```bash
git status
```

If clean: done. If any unstaged changes remain, commit them.

---

## Deployment Checklist (Render)

After merging, set these env vars in your Render dashboard before deploying:

- [ ] `RH_ENABLED=true`
- [ ] `RH_USERNAME=your@email.com`
- [ ] `RH_PASSWORD=yourpassword`
- [ ] `RH_LEVERAGE_FACTOR=0.3` (adjust to your risk preference)
- [ ] `RH_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`

On first deploy:
1. Deploy the service
2. Watch your Robinhood Discord channel — you'll see: *"Robinhood session unavailable — POST /robinhood-auth with your SMS code to activate."*
3. Wait for the SMS code from Robinhood
4. Run: `curl -X POST https://your-render-url/robinhood-auth -H "Content-Type: application/json" -d '{"secret":"YOUR_WEBHOOK_SECRET","sms_code":"123456"}'`
5. You'll see: *"Robinhood session restored ✅"* in your Discord channel
6. Done — trades now mirror to Robinhood automatically
