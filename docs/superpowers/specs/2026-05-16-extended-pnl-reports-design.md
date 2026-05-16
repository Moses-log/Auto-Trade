# Extended P&L Reports — Design Spec

**Date:** 2026-05-16
**Feature:** Add monthly, yearly, YTD, and all-time P&L reports — auto-scheduled and on-demand

---

## Problem

The system only reports daily and weekly P&L. Longer-period views (monthly, yearly, YTD, all-time) require manual inspection of Alpaca. These should be available both on a schedule and on demand via Discord.

---

## Goal

Add four new report types that follow the exact same pattern as existing daily/weekly reports. Two auto-schedule, two are on-demand only.

---

## Report Types

| Report | Auto-schedule | On demand |
|---|---|---|
| Daily | ✅ Mon–Fri 4:00 PM ET | ✅ |
| Weekly | ✅ Friday 4:01 PM ET | ✅ |
| Monthly | ✅ Last trading day of month 4:05 PM ET | ✅ |
| Yearly (1 Year) | ✅ Last trading day of Dec 4:05 PM ET | ✅ |
| YTD | ❌ | ✅ |
| All Time | ❌ | ✅ |

---

## P&L Calculation

All reports use **Option A**: portfolio value now vs portfolio value at start of period. Ignores deposits/withdrawals — measures pure performance.

Uses Alpaca's `get_portfolio_history` with `1D` timeframe for all new reports. SPY comparison via yfinance.

| Function | Alpaca period | yfinance period | Date label |
|---|---|---|---|
| `send_monthly_report()` | `"1M"` | `"1mo"` | "Month of May 2026" |
| `send_yearly_report()` | `"1A"` | `"1y"` | "Year 2026" |
| `send_ytd_report()` | `"{N}D"` (days since Jan 1) | `"ytd"` | "YTD Jan 1 – May 16, 2026" |
| `send_alltime_report()` | `"all"` | `"max"` | "All Time since May 9, 2026" |

**YTD period:** calculated as `(today - Jan 1).days` and passed as `f"{days}D"` to Alpaca.

**All time start date:** first non-zero equity value date from the `all` history response, formatted as "since Month D, YYYY".

---

## Architecture

### New functions in `app/pnl.py`

Four new async functions following the exact same structure as `send_daily_report` and `send_weekly_report`:

```python
async def send_monthly_report() -> None: ...
async def send_yearly_report() -> None: ...
async def send_ytd_report() -> None: ...
async def send_alltime_report() -> None: ...
```

Each:
1. Builds a date label string
2. Calls `get_portfolio_history(period, timeframe="1D")`
3. Calls `_compute_pnl(history, period_name)`
4. Calls `compute_spy_pct(yfinance_period)`
5. Calls `_format_message(result, label, date_str, spy_pct)`
6. Calls `await notify(msg)`

No changes to `_compute_pnl` or `_format_message` — they are period-agnostic.

---

### Scheduled auto-sends in `app/scheduler.py`

APScheduler has no native "last trading day of month/year" trigger. Use a **conditional daily check** pattern:

Add one new job `period_pnl_check` firing Mon–Fri at **4:05 PM ET**. The handler:

```python
async def check_period_reports() -> None:
    today = date.today()
    next_trading_day = get_next_trading_day()
    if next_trading_day.month != today.month:
        await send_monthly_report()
    if next_trading_day.year != today.year:
        await send_yearly_report()
```

Uses existing `get_next_trading_day()` from `alpaca_client.py` — correctly handles weekends and market holidays via Alpaca's calendar API.

New `setup_jobs()` job:

```
period_pnl_check  Mon–Fri 16:05 ET  → check_period_reports()
```

---

### Discord slash command updates

**`app/discord_commands.py` — `handle_report`**

Add routing:
```
"monthly"  → send_monthly_report()
"ytd"      → send_ytd_report()
"1year"    → send_yearly_report()
"alltime"  → send_alltime_report()
```

**`scripts/register_commands.py`** — update `/report` choices:

| Value | Display name |
|---|---|
| `daily` | Daily |
| `weekly` | Weekly |
| `monthly` | Monthly |
| `ytd` | Year to Date |
| `1year` | 1 Year |
| `alltime` | All Time |
| `both` | Daily & Weekly |

**`app/main.py` — `POST /run-report`**

Accept new values: `"monthly"`, `"ytd"`, `"1year"`, `"alltime"`. Update the validation allowlist from `("daily", "weekly", "both")` to include all new types.

---

## Files Changed

| File | Change |
|---|---|
| `app/pnl.py` | Add `send_monthly_report`, `send_yearly_report`, `send_ytd_report`, `send_alltime_report`, `check_period_reports` |
| `app/scheduler.py` | Add `period_pnl_check` job in `setup_jobs()` |
| `app/discord_commands.py` | Add routing for 4 new report types in `handle_report` |
| `app/main.py` | Expand `/run-report` allowlist |
| `scripts/register_commands.py` | Add 4 new choices to `/report` command |

---

## Error Handling

Same pattern as existing reports — each function wraps in try/except, logs the error, and sends a `⚠️` message to Discord on failure.

---

## After Deployment

Re-run `scripts/register_commands.py` once to push updated slash command choices to Discord.

---

## Out of Scope

- Per-investor breakdown for extended periods
- Custom date range reports
- Chart/graph attachments
