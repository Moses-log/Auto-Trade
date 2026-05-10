# Trade Alert Discord Notification — Design Spec
**Date:** 2026-05-10
**Project:** Moses-log/Auto-Trade

---

## Overview

After every trade executes via `POST /webhook`, send a Discord message to a dedicated trades channel (`DISCORD_TRADES_WEBHOOK_URL`) showing ticker, action, fill price, quantity, position size after trade, and timestamp. On sells, include realized P&L (dollar + percentage).

---

## Architecture

A new `app/trade_notifier.py` module owns all trade notification logic. `app/main.py` calls one function after `execute_action()` completes. Notification failure never blocks the 200 response.

---

## Data Flow

### On any trade:
1. If action is a sell → fetch current position from Alpaca BEFORE `execute_action()` to capture `avg_entry_price` and `qty`
2. `execute_action()` runs → returns `result` dict containing `alpaca_order_id`
3. Fetch full order from Alpaca by ID → get `filled_avg_price` and `filled_qty`
4. Fetch current position after trade → get new position size (0 if fully closed)
5. If sell → compute P&L from pre-trade position and fill price
6. Format message → send to `DISCORD_TRADES_WEBHOOK_URL`

### P&L formula (sell only):
```
dollar_pnl = (fill_price - avg_entry_price) × filled_qty
pct_pnl    = (fill_price - avg_entry_price) / avg_entry_price × 100
```

### Actions that trigger pre-trade position fetch (for P&L):
`SELL`, `CLOSE_LONG`, `CLOSE_SHORT`, `REVERSE_TO_LONG`, `REVERSE_TO_SHORT` — any action that closes or partially closes a position. `BUY` and `BASE_ENTRY` do not trigger P&L calculation.

### Graceful degradation:
- If `get_order()` fails → show `price≈{alert_price}` from TradingView payload
- If `get_position()` fails → omit position size line
- If P&L data unavailable on sell → omit P&L line
- Notification failure always logged as warning, never raises

---

## Discord Message Format

**Buy:**
```
🟢 **BUY — SPY**
Qty: 5 shares @ $537.42
Position: 5 shares
🕐 2:32 PM ET — May 10, 2026
```

**Sell:**
```
🔴 **SELL — SPY**
Qty: 5 shares @ $551.80
Position: 0 shares
P&L: +$71.90 (+2.68%)
🕐 3:45 PM ET — May 10, 2026
```

---

## New Module: `app/trade_notifier.py`

Single public function:

```python
async def notify_trade(
    ticker: str,
    action: str,
    result: dict,
    alert_price: Optional[float],
    avg_entry_price: Optional[float],
) -> None
```

Internally:
- Extracts `alpaca_order_id` from `result["orders"][0]` if available
- Calls `get_order(order_id)` → `filled_avg_price`, `filled_qty`
- Calls `get_position(ticker)` → current qty after trade
- Computes P&L if sell and `avg_entry_price` is available
- Formats message
- Calls `notify_trades(message)`

---

## New Alpaca Client Methods (`app/trading/alpaca_client.py`)

```python
def get_order(order_id: str) -> Optional[Order]
def get_position(ticker: str) -> Optional[Position]
```

Both return `None` on failure (not found or API error), logged as warning.

---

## Config Changes (`app/config.py`)

```python
discord_trades_webhook_url: Optional[str] = None
```

If not set → trade notifications skipped silently with warning logged.

---

## Notifications Changes (`app/notifications.py`)

```python
async def notify_trades(message: str) -> None
```

Routes to `settings.discord_trades_webhook_url`. Falls back to nothing (not to main channel) — trade alerts are opt-in.

---

## `app/main.py` Changes

In `/webhook` handler, after successful `execute_action()`:

```python
# capture pre-trade position for P&L on sells
avg_entry_price = None
if is_sell_action(payload.action):
    pos = get_position(payload.ticker)
    avg_entry_price = float(pos.avg_entry_price) if pos else None

result = await execute_action(payload)

# fire-and-forget trade notification
asyncio.create_task(
    notify_trade(payload.ticker, payload.action, result, payload.price, avg_entry_price)
)
```

---

## Files Changed / Created

| File | Change |
|------|--------|
| `app/trade_notifier.py` | New — fetch fill details, compute P&L, format + send message |
| `app/trading/alpaca_client.py` | Add `get_order()` and `get_position()` |
| `app/config.py` | Add `discord_trades_webhook_url: Optional[str] = None` |
| `app/notifications.py` | Add `notify_trades()` |
| `app/main.py` | Capture pre-trade position for sells; call `notify_trade()` after execute |
| `tests/test_trade_notifier.py` | New — unit tests for trade_notifier |
| `tests/test_webhook.py` | Update — add `notify_trade` mock to existing webhook tests |

---

## Setup Required (Render)

Add environment variable:
- `DISCORD_TRADES_WEBHOOK_URL` = Discord webhook URL for the trades channel

If not set, trade notifications are silently skipped.
