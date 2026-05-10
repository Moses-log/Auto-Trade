# Trade Alert Discord Notification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After every trade executes on Alpaca, send a Discord message to a dedicated trades channel showing ticker, action, fill price, quantity, position size, timestamp, and P&L on sells.

**Architecture:** A new `app/trade_notifier.py` module owns all trade notification logic. It fetches fill details from Alpaca after execution, computes P&L for sells using the pre-trade avg entry price captured in `app/main.py`, formats the message, and sends it via `notify_trades()`. The notification is fire-and-forget (`asyncio.create_task`) so it never delays the 200 response.

**Tech Stack:** Python, FastAPI, httpx, Alpaca SDK, pytz, pytest, pytest-asyncio

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `app/trading/alpaca_client.py` | Modify | Add `get_order(order_id)` |
| `app/config.py` | Modify | Add `discord_trades_webhook_url` |
| `app/notifications.py` | Modify | Add `notify_trades()` |
| `app/trade_notifier.py` | Create | Fetch fill details, compute P&L, format + send message |
| `app/main.py` | Modify | Capture pre-trade position for sells; fire notify_trade after execute |
| `tests/test_pnl.py` | Modify | Add `get_order` tests |
| `tests/test_trade_notifier.py` | Create | Unit tests for trade_notifier |
| `tests/test_webhook.py` | Modify | Patch `notify_trade` in existing tests |

---

### Task 1: `app/trading/alpaca_client.py` — add `get_order()`

**Files:**
- Modify: `app/trading/alpaca_client.py`
- Modify: `tests/test_pnl.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pnl.py`:

```python
# ── get_order tests ───────────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_client")
def test_get_order_returns_order_on_success(mock_get_client):
    from app.trading.alpaca_client import get_order
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    fake_order = MagicMock()
    fake_order.filled_avg_price = "537.42"
    fake_order.filled_qty = "5"
    mock_client.get_order_by_id.return_value = fake_order
    result = get_order("abc-123")
    assert result is fake_order
    mock_client.get_order_by_id.assert_called_once_with("abc-123")


@patch("app.trading.alpaca_client.get_client")
def test_get_order_returns_none_on_exception(mock_get_client):
    from app.trading.alpaca_client import get_order
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_order_by_id.side_effect = Exception("not found")
    result = get_order("bad-id")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_pnl.py::test_get_order_returns_order_on_success -v
```

Expected: `ImportError: cannot import name 'get_order'`

- [ ] **Step 3: Add `get_order` to `app/trading/alpaca_client.py`**

Append after the existing `get_position` function:

```python
def get_order(order_id: str) -> Optional[Order]:
    try:
        return get_client().get_order_by_id(order_id)
    except Exception as exc:
        log.warning("Could not fetch order %s: %s", order_id, exc)
        return None
```

`Optional` and `Order` are already imported in `alpaca_client.py`. `log` is already defined. `get_client` is already defined.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_pnl.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
cd C:\Users\moses\Auto-Trade && git add app/trading/alpaca_client.py tests/test_pnl.py && git commit -m "feat: add get_order() to alpaca_client"
```

---

### Task 2: `app/config.py` + `app/notifications.py`

**Files:**
- Modify: `app/config.py`
- Modify: `app/notifications.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investors.py`:

```python
def test_config_discord_trades_webhook_url_defaults_to_none():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x")
    assert s.discord_trades_webhook_url is None


def test_config_accepts_discord_trades_webhook_url():
    from app.config import Settings
    s = Settings(
        alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x",
        discord_trades_webhook_url="https://discord.com/api/webhooks/trades/abc",
    )
    assert s.discord_trades_webhook_url == "https://discord.com/api/webhooks/trades/abc"


@pytest.mark.asyncio
async def test_notify_trades_posts_to_trades_webhook():
    from unittest.mock import AsyncMock, MagicMock, patch
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=MagicMock())
    with patch("app.notifications.settings") as mock_settings:
        mock_settings.discord_trades_webhook_url = "https://discord.com/trades"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.notifications import notify_trades
            await notify_trades("test trade message")
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://discord.com/trades"


@pytest.mark.asyncio
async def test_notify_trades_skips_when_url_not_set():
    from unittest.mock import AsyncMock, patch
    with patch("app.notifications.settings") as mock_settings:
        mock_settings.discord_trades_webhook_url = None
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.notifications import notify_trades
            await notify_trades("test trade message")
    mock_client.post.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_investors.py::test_config_discord_trades_webhook_url_defaults_to_none -v
```

Expected: `AttributeError` — field does not exist on `Settings`.

- [ ] **Step 3: Add field to `app/config.py`**

Inside the `Settings` class, add after `github_repo`:

```python
discord_trades_webhook_url: Optional[str] = None
```

- [ ] **Step 4: Add `notify_trades` to `app/notifications.py`**

Append after `notify_investors`:

```python
async def notify_trades(message: str) -> None:
    url = settings.discord_trades_webhook_url
    if not url:
        logger.warning("DISCORD_TRADES_WEBHOOK_URL not set; skipping trade notification")
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        logger.warning("Trade Discord notification failed: %s", exc)
```

`httpx`, `logger`, and `settings` are already imported in `notifications.py`.

- [ ] **Step 5: Run all tests**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 6: Commit**

```bash
cd C:\Users\moses\Auto-Trade && git add app/config.py app/notifications.py tests/test_investors.py && git commit -m "feat: add discord_trades_webhook_url config and notify_trades()"
```

---

### Task 3: `app/trade_notifier.py` — new module

**Files:**
- Create: `app/trade_notifier.py`
- Create: `tests/test_trade_notifier.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trade_notifier.py`:

```python
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


# ── _format_trade_message tests ───────────────────────────────────────────────

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
    assert "0" in msg  # position qty


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


# ── notify_trade tests ────────────────────────────────────────────────────────

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
                    avg_entry_price=537.42,  # pre-trade entry price
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
                # Must not raise
                await notify_trade(
                    ticker="SPY", action="BUY",
                    result={"orders": []},
                    alert_price=537.00,
                    avg_entry_price=None,
                )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_trade_notifier.py -v
```

Expected: `ModuleNotFoundError` — `app.trade_notifier` does not exist.

- [ ] **Step 3: Create `app/trade_notifier.py`**

```python
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz

from app.notifications import notify_trades
from app.trading.alpaca_client import get_order, get_position

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

_BUY_ACTIONS = {"BUY", "BASE_ENTRY", "ADD_LEVERAGE"}


def _format_trade_message(
    ticker: str,
    action: str,
    filled_price: Optional[float],
    alert_price: Optional[float],
    filled_qty: Optional[float],
    position_qty: float,
    dollar_pnl: Optional[float],
    pct_pnl: Optional[float],
) -> str:
    is_buy = action.upper() in _BUY_ACTIONS
    emoji = "🟢" if is_buy else "🔴"

    if filled_price is not None:
        price_str = f"${filled_price:,.2f}"
    elif alert_price is not None:
        price_str = f"≈${alert_price:,.2f}"
    else:
        price_str = "unknown"

    qty_str = f"{filled_qty:.0f}" if filled_qty is not None else "?"

    now = datetime.now(ET)
    hour = int(now.strftime("%I"))
    time_str = f"{hour}:{now.strftime('%M %p')} ET — {now.strftime('%B')} {now.day}, {now.year}"

    lines = [
        f"{emoji} **{action.upper()} — {ticker}**",
        f"Qty: {qty_str} shares @ {price_str}",
        f"Position: {position_qty:.0f} shares",
    ]

    if dollar_pnl is not None and pct_pnl is not None:
        sign = "+" if dollar_pnl >= 0 else ""
        lines.append(f"P&L: {sign}${dollar_pnl:,.2f} ({sign}{pct_pnl:.2f}%)")

    lines.append(f"🕐 {time_str}")
    return "\n".join(lines)


async def notify_trade(
    ticker: str,
    action: str,
    result: dict,
    alert_price: Optional[float],
    avg_entry_price: Optional[float],
) -> None:
    try:
        filled_price: Optional[float] = None
        filled_qty: Optional[float] = None

        orders = result.get("orders", [])
        if orders:
            order_id = orders[0].get("alpaca_order_id")
            if order_id:
                order = get_order(order_id)
                if order and order.filled_avg_price:
                    filled_price = float(order.filled_avg_price)
                    filled_qty = float(order.filled_qty) if order.filled_qty else None

        position_qty = 0.0
        pos = get_position(ticker)
        if pos and pos.qty:
            position_qty = float(pos.qty)

        dollar_pnl: Optional[float] = None
        pct_pnl: Optional[float] = None
        if avg_entry_price and filled_price and filled_qty and avg_entry_price != 0:
            dollar_pnl = (filled_price - avg_entry_price) * filled_qty
            pct_pnl = (filled_price - avg_entry_price) / avg_entry_price * 100

        message = _format_trade_message(
            ticker=ticker,
            action=action,
            filled_price=filled_price,
            alert_price=alert_price,
            filled_qty=filled_qty,
            position_qty=position_qty,
            dollar_pnl=dollar_pnl,
            pct_pnl=pct_pnl,
        )
        await notify_trades(message)

    except Exception as exc:
        log.warning("Trade notification failed: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_trade_notifier.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
cd C:\Users\moses\Auto-Trade && git add app/trade_notifier.py tests/test_trade_notifier.py && git commit -m "feat: add trade_notifier.py with notify_trade() and message formatting"
```

---

### Task 4: `app/main.py` — wire up in `/webhook`

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_webhook.py`

- [ ] **Step 1: Update `tests/test_webhook.py`**

Tests that reach `execute_action` and return 200 need `app.main.notify_trade` patched. Add `@patch("app.main.notify_trade", new_callable=AsyncMock)` as an additional decorator to each such test, and add `mock_notify_trade` as a corresponding argument. The tests that need this are any test that patches `get_client` AND submits a successful trade (buy, sell, close_long, etc). Tests that return 401, 422, 400, or "duplicate" status do NOT need this patch since they return before `execute_action` is called.

Also add `from unittest.mock import AsyncMock` to the imports at the top of the file if not already present.

Example of how a patched test looks — before:
```python
@patch("app.trading.alpaca_client.get_client")
def test_buy_order_executes(mock_get_client):
    payload = _load_sample("buy")
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.submit_order.return_value = _mock_order()
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
```

After:
```python
@patch("app.main.notify_trade", new_callable=AsyncMock)
@patch("app.trading.alpaca_client.get_client")
def test_buy_order_executes(mock_get_client, mock_notify_trade):
    payload = _load_sample("buy")
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.submit_order.return_value = _mock_order()
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
```

NOTE: `@patch` decorators apply bottom-up, so the bottom-most patch becomes the first function argument. `mock_get_client` stays first, `mock_notify_trade` is added last.

Additionally add two new tests at the end of the file:

```python
@patch("app.main.notify_trade", new_callable=AsyncMock)
@patch("app.trading.alpaca_client.get_client")
def test_notify_trade_called_after_successful_trade(mock_get_client, mock_notify_trade):
    payload = _load_sample("buy")
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.submit_order.return_value = _mock_order()
    client.post("/webhook", json=payload)
    mock_notify_trade.assert_called_once()
    call_kwargs = mock_notify_trade.call_args[1]
    assert call_kwargs["ticker"] == payload["ticker"]
    assert call_kwargs["action"] == "BUY"


@patch("app.main.notify_trade", new_callable=AsyncMock)
@patch("app.main.get_position")
@patch("app.trading.alpaca_client.get_client")
def test_pre_trade_position_fetched_for_sell(mock_get_client, mock_get_position, mock_notify_trade):
    payload = _load_sample("sell")
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.submit_order.return_value = _mock_order(side="sell")
    mock_get_position.return_value = None
    client.post("/webhook", json=payload)
    mock_get_position.assert_called_once_with(payload["ticker"])
```

- [ ] **Step 2: Run webhook tests to verify the two new tests fail**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_webhook.py -v
```

Expected: `test_notify_trade_called_after_successful_trade` and `test_pre_trade_position_fetched_for_sell` FAIL; existing tests that now lack the `notify_trade` patch will also fail.

- [ ] **Step 3: Update `app/main.py`**

Add these imports alongside existing imports at the top:

```python
import asyncio
from typing import Optional

from app.models import AlertPayload, DepositRequest, TradingAction
from app.trade_notifier import notify_trade
from app.trading.alpaca_client import get_latest_price, get_position
```

NOTE: `asyncio` and `Optional` are new top-level imports. `TradingAction` is new — add it to the existing `from app.models import ...` line. `notify_trade` is new — add a new import line. `get_position` is new — add it to the existing `from app.trading.alpaca_client import ...` line. Keep all other existing imports unchanged.

Define this constant after the imports (before the `app = FastAPI(...)` line):

```python
_SELL_ACTIONS = {
    TradingAction.SELL,
    TradingAction.CLOSE_LONG,
    TradingAction.CLOSE_SHORT,
    TradingAction.REVERSE_TO_LONG,
    TradingAction.REVERSE_TO_SHORT,
}
```

Inside the `/webhook` handler, replace the execute trade section (step 5) with:

```python
    # ── 5. Execute trade ──────────────────────────────────────────────────────
    try:
        # Capture pre-trade entry price for P&L on sells
        avg_entry_price: Optional[float] = None
        if payload.action in _SELL_ACTIONS:
            pos = get_position(payload.ticker)
            if pos and pos.avg_entry_price:
                avg_entry_price = float(pos.avg_entry_price)

        result = await execute_action(payload)
        mark_processed(payload)

        log.info(
            "Trade executed",
            extra={"ticker": payload.ticker, "action": payload.action, "result": result},
        )

        await notify(
            f"✅ <b>{payload.action.upper()}</b> {payload.ticker} "
            f"| qty={payload.contracts} | price≈{payload.price}"
        )

        asyncio.create_task(
            notify_trade(
                ticker=payload.ticker,
                action=str(payload.action).upper(),
                result=result,
                alert_price=payload.price,
                avg_entry_price=avg_entry_price,
            )
        )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "result": result},
        )
```

Leave all exception handlers (ValueError, APIError, Exception) unchanged.

- [ ] **Step 4: Run webhook tests — all must pass**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_webhook.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Run full test suite**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest -v 2>&1 | tail -5
```

Expected: all tests PASSED.

- [ ] **Step 6: Commit and push**

```bash
cd C:\Users\moses\Auto-Trade && git add app/main.py tests/test_webhook.py && git commit -m "feat: fire trade Discord notification after every /webhook trade" && git push
```

---

## Done

Once deployed to Render, add `DISCORD_TRADES_WEBHOOK_URL` to the service's environment variables. Every trade fired from TradingView will automatically send a Discord message to that channel with fill details and P&L on sells.
