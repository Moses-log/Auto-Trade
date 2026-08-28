# Alpaca Hedge-Fund Trade Notifier — Design

**Date:** 2026-08-27
**Status:** Approved for planning

## Problem

The shared Alpaca account runs an external strategy (orders arrive with
`source: access_key` / `dashboard`) that trades non-SPY symbols long and
short, alongside Kimi's own SPY strategy. These non-SPY trades are invisible
to Discord today. We want:

1. A live Discord notification for every **filled** non-SPY trade — on both
   entry and exit — stating shares, dollars, price, and time.
2. On exit, a **WIN/LOSS** verdict with percentage and dollar P&L, and a
   per-investor dollar split of that trade's P&L (investors participate by
   size — this is a hedge fund).
3. A **daily recap** at midnight CT: every non-SPY fill that day, total
   gain/loss, wins/losses, and win rate.
4. The investor **equity breakdown** report extended to show each investor's
   non-SPY realized-P&L contribution.

Kimi does not place these orders, so it cannot hook its own order logic. It
must **poll** Alpaca for filled orders and reconstruct round trips itself.

## Non-Goals

- Placing or managing the non-SPY trades (an external system owns that).
- Notifying on non-filled orders (`new`, `canceled`, `expired`, partials with
  zero filled qty are ignored).
- SPY trades — already handled by the existing `trade_notifier` path.

## Global Constraints

- Python 3.12, FastAPI service, deployed on Render with persistent disk at
  `/data/`.
- Async throughout; reuse `app/notifications.py` HTTP client and the
  APScheduler singleton in `app/scheduler.py` (timezone `America/New_York`).
- State files live on `/data/` and mirror the atomic tmp-write-then-replace
  pattern of `app/rh_trade_record.py`.
- Money formatted `$1,234.56`; times displayed in `America/Chicago` (CT) to
  match existing trade messages.
- New config fields are `Optional[str] = None`; the feature no-ops (logs a
  warning) when a webhook is unset, exactly like `notify_trades`.

## Architecture

```
Alpaca REST --poll every 2 min--> alpaca_hf_notifier.poll_and_notify()
   get_orders_filled_range(after=last_seen, until=now)
        |  filter: symbol != "SPY", status == FILLED, id not seen
        v
   classify by position_intent --> alpaca_hf_record (FIFO open-lot queue)
        |                               |
        |  open fill                    |  close fill -> pop FIFO lots -> realized P&L
        v                               v
   notify open message            notify close message (WIN/LOSS + investor split)
        +---------------+---------------+
                        v
                daily fills log (for recap)

00:00 CT cron --> alpaca_hf_notifier.send_daily_recap()
Investor report --> compute_breakdown() + non-SPY contribution line
```

### Polling & dedup

- New APScheduler **interval job**, every 2 minutes, calls
  `poll_and_notify()`. Runs the full clock (fills occur pre-market and
  after-hours in the sample data), guarded by a module-level asyncio lock so
  overlapping fires can't double-process.
- `last_seen` is a UTC ISO timestamp persisted in the state file. Query window
  is `[last_seen - 5 min, now]` (overlap guards clock skew / late fills);
  dedup by Alpaca order id, which is the authoritative guard. After a
  successful pass, advance `last_seen` to the newest processed fill time
  (never past `now`).
- First-ever run: seed `last_seen = now` and process nothing historical (avoid
  a backfill flood). Record this seed on startup.

### Classification (long vs short)

Alpaca `order.side` + `order.position_intent`:

| position_intent | side | meaning        | direction | role  |
|-----------------|------|----------------|-----------|-------|
| buy_to_open     | buy  | open long      | LONG      | OPEN  |
| sell_to_close   | sell | close long     | LONG      | CLOSE |
| sell_to_open    | sell | open short     | SHORT     | OPEN  |
| buy_to_close    | buy  | close short    | SHORT     | CLOSE |

If `position_intent` is missing on an order, fall back to inference from side
against the current open-lot queue for that symbol (a `buy` with an open short
lot is a close; otherwise an open). Log any order that can't be classified and
skip it rather than guess.

### Round-trip pairing (FIFO per symbol)

State holds `open_lots[symbol]` = ordered list of
`{direction, qty, entry_price, entry_ts, order_id}`.

- **OPEN fill:** append a lot (`filled_qty`, `avg_fill_price`).
- **CLOSE fill:** consume lots FIFO for that symbol and direction until the
  close qty is exhausted (a close may span multiple opens; an open may be
  closed by multiple closes -> leave a partial lot). For each consumed
  (portion of a) lot compute realized P&L:
  - LONG: `(exit_price - entry_price) * matched_qty`
  - SHORT: `(entry_price - exit_price) * matched_qty`
  - The close's total realized P&L = sum over consumed lots.
  - `pct = realized_pnl / (sum entry_price * matched_qty) * 100`.
- **Win** = total realized P&L for the close event > 0.
- If a close arrives with no open lot on record (e.g., position opened before
  the feature went live), notify the exit with `P&L: n/a (no recorded entry)`
  and count it as neither win nor loss.

### Per-investor split

On a close, load investors + real Alpaca equity, call
`compute_breakdown(...)`, and allocate the trade's realized P&L by each
investor's `portfolio_share`:
`investor_pnl = realized_pnl * portfolio_share / 100`. List each investor and
their dollar share in the close notification.

## Data / State — `app/alpaca_hf_record.py`

JSON at `ALPACA_HF_RECORD_PATH` (default `/data/alpaca_hf_record.json`),
atomic writes, asyncio lock. Shape:

```json
{
  "last_seen": "2026-08-27T14:32:42+00:00",
  "seen_order_ids": ["<id>", "..."],
  "open_lots": {
    "QCOM": [{"direction": "LONG", "qty": 12.0, "entry_price": 164.37,
              "entry_ts": "...", "order_id": "..."}]
  },
  "closed_trades": [
    {"symbol": "QCOM", "direction": "LONG", "qty": 12.0,
     "entry_price": 164.37, "exit_price": 164.84, "realized_pnl": 5.64,
     "pct": 0.29, "is_win": true, "closed_ts": "..."}
  ],
  "daily_fills": [
    {"symbol": "QCOM", "role": "OPEN", "direction": "LONG", "qty": 12.0,
     "price": 164.37, "notional": 1972.44, "ts": "..."}
  ],
  "wins": 0,
  "losses": 0
}
```

- `seen_order_ids` is bounded (keep last ~2000; trim oldest).
- `daily_fills` is cleared by the recap job after it posts (it is the
  "today" buffer). `closed_trades` accrues for lifetime win/loss and the
  investor-contribution line; may be capped later if it grows large.

Public functions (async unless noted):
- `load_state() -> dict` / `save_state(state)` — internal.
- `get_last_seen() -> datetime | None`, `set_last_seen(dt)`.
- `is_seen(order_id) -> bool`, `mark_seen(order_id)`.
- `record_open(symbol, direction, qty, price, ts, order_id) -> None`.
- `record_close(symbol, direction, qty, exit_price, ts) -> CloseResult`
  where `CloseResult` = `{matched_qty, realized_pnl, pct, is_win,
  unmatched_qty}`. Updates `open_lots`, `closed_trades`, `wins`/`losses`.
- `record_daily_fill(fill_dict) -> None`.
- `pop_daily_fills() -> list` — returns and clears `daily_fills`.
- `contribution_total() -> float` — sum of `closed_trades` realized P&L,
  for the breakdown line.

## Notifications — additions to `app/notifications.py`

- `notify_hf_trade(message: str)` -> `ALPACA_HF_TRADES_WEBHOOK_URL`, warn +
  skip if unset.
- `notify_hf_recap(message: str)` -> `ALPACA_HF_RECAP_WEBHOOK_URL`, warn +
  skip if unset.

Both follow the exact structure of the existing `notify_trades` function.

### Message formats (CT time)

**Open:**
```
LONG OPEN — QCOM
12 shares @ $164.77 ($1,977.24)
3:32 PM CT — August 27, 2026
```
(SHORT open uses the red emoji and `SHORT OPEN`.)

**Close (win shown; loss uses red emoji and "LOSS"):**
```
LONG CLOSE — QCOM  WIN
Exit: 12 shares @ $164.84 ($1,978.08)
P&L: +$5.64 (+0.29%)
10:11 AM CT — August 27, 2026
Investor split:
  - Alice: +$3.10
  - Bob:   +$2.54
```

**Daily recap:**
```
Non-SPY Recap — August 27, 2026 (CT)
Fills today: 14  (7 opens / 7 closes)
Closed round-trips: 7 — 5 W / 2 L (71.4% win rate)
Total realized P&L: +$41.28
Fills:
  QCOM LONG OPEN  12 @ $164.37 ($1,972.44)
  QCOM LONG CLOSE 12 @ $164.84 (+$5.64)
  ...
```
Win-rate/W-L come only from round-trips **closed** today; the fills list shows
every fill (opens + closes) per the chosen recap scope.

## Scheduler — `app/scheduler.py`

- Add interval job: `poll_and_notify` every 2 minutes (`IntervalTrigger`),
  `max_instances=1`, wrapped in the existing `_profiled` helper and a
  try/except so a transient Alpaca error never crashes the scheduler.
- Add cron job: `send_daily_recap` at `00:00 America/Chicago`. Register with an
  explicit `CronTrigger(hour=0, minute=0, timezone="America/Chicago")` so it is
  independent of the scheduler's ET default.

## Investor breakdown edit — `app/investors.py` / `app/pnl.py`

- `compute_breakdown` already yields `portfolio_share`. Add an optional
  parameter `nonspy_pnl: float = 0.0`; when provided, populate a new
  `InvestorResult.nonspy_contribution = nonspy_pnl * portfolio_share / 100`.
- `send_investor_report()` fetches lifetime non-SPY realized P&L from
  `alpaca_hf_record.contribution_total()` and passes it in, rendering a new
  line per investor: `Non-SPY contribution: +$X`. Default 0.0 keeps existing
  callers and tests unchanged.

## Config — `app/config.py`

```python
alpaca_hf_trades_webhook_url: Optional[str] = None
alpaca_hf_recap_webhook_url: Optional[str] = None
```

## Error Handling

- Alpaca fetch failure in a poll: log warning, leave `last_seen` unadvanced,
  return (next tick retries the same window). No partial state commit.
- Webhook post failure: logged and swallowed (matches existing notifiers); the
  fill is still marked seen so we don't spam retries — a missed message is
  preferable to a loop.
- Malformed / unclassifiable order: logged and skipped, marked seen.
- State file corrupt/unreadable: fall back to empty state, log error, reseed
  `last_seen = now` (same defensive pattern as `rh_trade_record._load`).

## Testing

Per module, mirroring existing test style:
- **record:** FIFO pairing across single/multiple/partial lots; long vs short
  P&L sign; win/loss counting; close-without-open -> n/a; dedup; daily-fills
  buffer clear; atomic persistence round-trip.
- **notifier:** classification table; open vs close routing; investor split
  math; recap aggregation (counts, win rate, totals) using the 2026-08-27
  sample orders as a fixture; SPY exclusion; first-run seeding (no backfill).
- **notifications:** new funcs post to correct webhook, no-op when unset.
- **breakdown:** `nonspy_contribution` split by share; default 0.0 preserves
  prior output.

## Rollout

Ship dark: without the two webhook env vars set, poller still runs and builds
state but posts nothing. Set `ALPACA_HF_TRADES_WEBHOOK_URL` and
`ALPACA_HF_RECAP_WEBHOOK_URL` in Render to go live.
