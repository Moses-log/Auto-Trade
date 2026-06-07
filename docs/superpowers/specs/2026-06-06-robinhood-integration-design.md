# Robinhood Integration Design

**Date:** 2026-06-06
**Status:** Approved

## Overview

Extend the Auto-Trade FastAPI service to execute trades on Robinhood in parallel with Alpaca whenever a TradingView webhook fires. Both brokers operate independently — a failure on one never affects the other.

---

## Constraints & Context

- Robinhood does not support authenticator-app (TOTP) 2FA — SMS only.
- Authentication uses `robin_stocks`, which caches a session token in a pickle file after the first SMS-verified login.
- The session token auto-refreshes but eventually expires (every few weeks), requiring a one-time manual re-authentication via a new `/robinhood-auth` endpoint.
- The service is deployed on Render with a persistent disk at `/data` — the pickle file lives there alongside `investors.json` etc.

---

## Files Changed

| File | Change |
|---|---|
| `app/trading/robinhood_client.py` | **New** — auth, session management, order execution |
| `app/trading/order_logic.py` | **Modified** — call Robinhood in parallel with Alpaca in `execute_action()` |
| `app/main.py` | **Modified** — add `POST /robinhood-auth` endpoint |
| `app/config.py` | **Modified** — add `RH_*` settings |
| `app/notifications.py` | **Modified** — add `notify_robinhood()` posting to `RH_DISCORD_WEBHOOK_URL` |
| `requirements.txt` | **Modified** — add `robin_stocks` |

No other files are touched. Alpaca notifications, investor tracking, Discord commands, and the scheduler are all unchanged.

---

## New Environment Variables

```env
# Robinhood credentials
RH_USERNAME=your@email.com
RH_PASSWORD=yourpassword

# Position sizing: fraction of Robinhood buying power per trade
RH_LEVERAGE_FACTOR=0.3

# Kill switch — set to false to disable Robinhood entirely without removing config
RH_ENABLED=true

# Separate Discord channel for Robinhood trade alerts
RH_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Session token is stored at `/data/robinhood.pickle` (same persistent disk as other data files).

---

## Authentication Flow

### Startup
`robinhood_client.py` exposes a module-level `RobinhoodClient` instance. On first use it attempts to log in using the cached pickle file at `/data/robinhood.pickle`. If the file exists and the token is valid, no SMS is needed. If the file is missing or the token is expired, the client marks itself as `unavailable` and sends a Discord alert to `RH_DISCORD_WEBHOOK_URL`:

> "Robinhood session unavailable — POST /robinhood-auth with your SMS code to activate."

### Re-authentication Endpoint
```
POST /robinhood-auth
Body: { "secret": "<webhook_secret>", "sms_code": "123456" }
```

Protected by the same `webhook_secret` already used throughout the app. On success:
1. Calls `robin_stocks.login()` with the SMS code
2. Saves the new token to `/data/robinhood.pickle`
3. Marks the client as `available`
4. Sends Discord alert: "Robinhood session restored ✅"
5. Returns `200 { "status": "authenticated" }`

On failure:
- Wrong secret → `401`
- Invalid SMS code → `400 { "detail": "Invalid SMS code" }`

### Session Expiry During Trade
If a trade call returns an auth error, the client:
1. Marks itself `unavailable`
2. Sends Discord alert to `RH_DISCORD_WEBHOOK_URL`: "Robinhood session expired — POST /robinhood-auth to re-authenticate ⚠️"
3. Returns a `{"status": "skipped", "reason": "session expired"}` result — Alpaca is unaffected

---

## Trade Execution Flow

`execute_action()` in `order_logic.py` is updated to run both brokers in parallel:

```python
alpaca_result, rh_result = await asyncio.gather(
    _execute_alpaca(payload),
    _execute_robinhood(payload),
    return_exceptions=True,
)
```

`return_exceptions=True` ensures one broker raising never cancels the other.

### Robinhood Action Mapping

| TradingView Action | Robinhood Behavior |
|---|---|
| `BUY` | `(buying_power × RH_LEVERAGE_FACTOR) / current_price` → market buy |
| `SELL` | Close full long position for ticker (Robinhood doesn't use the `contracts` qty — full position close is the correct mirror given account-driven sizing) |
| `CLOSE_LONG` | Close full long position |
| `CLOSE_SHORT` | Close full short position |
| `REVERSE_TO_LONG` | Close short (if any) → market buy (same sizing as BUY) |
| `REVERSE_TO_SHORT` | Close long (if any) → skip short open (see note) |
| `ADD_LEVERAGE` | Same sizing formula as BUY |
| `REMOVE_LEVERAGE` | Close full position |
| `STOP_LOSS` | Close all positions for ticker |
| `BASE_ENTRY` | Skipped — same as Alpaca |

> **Note on shorting:** Standard Robinhood accounts do not support short selling. `CLOSE_SHORT`, `REVERSE_TO_SHORT`, and the short leg of any reverse action will be no-ops on Robinhood — they log a warning and skip without error. Only the close-long step of `REVERSE_TO_SHORT` will execute (if a long position exists).

### Position Sizing Detail (BUY / ADD_LEVERAGE / REVERSE)
```python
buying_power  = robin_stocks.get_buying_power()
current_price = robin_stocks.get_latest_price(ticker)
qty = floor((buying_power * settings.rh_leverage_factor) / current_price)
```
Fractional shares are not used — Robinhood's API does support them but we floor to whole shares for simplicity and consistency with the default Alpaca config.

---

## Notifications

Alpaca notifications are **completely unchanged** — same functions, same channel, same format.

Robinhood gets its own independent notification function `notify_robinhood(message)` in `notifications.py` that posts to `RH_DISCORD_WEBHOOK_URL`. If `RH_DISCORD_WEBHOOK_URL` is not set, Robinhood alerts fall back to the main `DISCORD_WEBHOOK_URL`.

**Robinhood trade alert format (posted to RH channel):**
```
BUY SPY — 12 shares @ $534.21 ✅
```
Or on failure:
```
BUY SPY — FAILED: session expired ❌
```

**Robinhood system alerts** (session expired, re-auth success) also go to `RH_DISCORD_WEBHOOK_URL`.

---

## Error Handling

| Situation | Behavior |
|---|---|
| Session expired on startup | Mark unavailable, Discord alert, trades skip Robinhood silently |
| Session expires mid-trade | Catch auth error, mark unavailable, Discord alert, return skipped result |
| API / network error | Log error, Discord alert with error detail, return failed result |
| `RH_ENABLED=false` | Skip all Robinhood logic silently, no alerts |
| Pickle file missing | Same as session expired — prompt re-auth |

Alpaca is never blocked or affected by any Robinhood error state.

---

## `robinhood_client.py` Interface

```python
class RobinhoodClient:
    available: bool

    def login_from_pickle(self) -> bool: ...
    def login_with_sms(self, sms_code: str) -> None: ...
    def get_buying_power(self) -> float: ...
    def get_latest_price(self, ticker: str) -> float: ...
    def place_market_order(self, ticker: str, side: str, qty: float) -> dict: ...
    def get_position(self, ticker: str) -> Optional[dict]: ...
    def close_position(self, ticker: str) -> Optional[dict]: ...

rh_client = RobinhoodClient()  # module-level singleton
```

`robin_stocks` is a synchronous library. All calls run in a thread pool via `asyncio.run_in_executor(None, ...)` so they don't block the FastAPI event loop.

---

## Dependencies

Add to `requirements.txt`:
```
robin-stocks>=2.1.0
```

No other new dependencies. `robin_stocks` has no mandatory sub-dependencies beyond `requests` which is already available transitively.

---

## Testing

- Unit tests mock `rh_client` at the `order_logic` level — same pattern as existing Alpaca mocks in `tests/test_webhook.py`
- Test cases: session unavailable skips gracefully, auth endpoint rejects bad secret, auth endpoint rejects bad SMS code, all action types map correctly
- No integration tests against live Robinhood — the unofficial API makes this impractical in CI
