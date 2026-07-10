# Kimi Inspection — Design Spec

## Goal

Kimi Manager currently only touches the portfolio once a month (`run_monthly_rebalance`, `app/claude_manager.py:733`). Between rebalances, nothing reacts to a bad earnings print, a guidance cut, or a macro shock hitting a held name — the position just sits until the 1st of next month. This spec adds a weekly "Inspection" pass over current holdings only, with authority to `SELL`, `TRIM`, or `DOUBLE_DOWN` mid-month, so Kimi can respond to events without waiting for a full rebalance, and so the monthly rebalance can lean on Inspection's more current view of each holding instead of re-deriving everything from scratch.

## Non-goals

- Inspection never buys a new ticker. New positions only originate from the monthly rebalance's candidate screening. This is enforced in code (Section 3), not just prompted.
- No premarket / extended-hours execution. Inspection runs after regular market open, so it reuses the existing market-order path (`_place_market_buy` / `_place_market_sell`, `robinhood_client.py:222,231`) unchanged — no new limit-order or extended-hours logic is needed.
- No change to the monthly rebalance's own research depth, portfolio-construction logic, or candidate-screening prompt.
- No Board-of-Directors / multi-agent restructuring — this is additive to the existing single-agent design, not a replacement of it.

## Current Behavior (for reference)

- `run_monthly_rebalance()` fires via `CronTrigger(day=1, hour=9, minute=35, timezone=ET)` (`scheduler.py:189-194`), calling `_claude_monthly_rebalance()` (`scheduler.py:153`), with **no market-open guard and no fallback if the 1st is a weekend/holiday** — unlike `_quarterly_tax_report` (`scheduler.py:110-138`), which correctly handles this via a `day="1-3"` cron window plus a `was_market_open_today()` check plus an Alpaca `GetCalendarRequest` lookback to detect whether an earlier day in the period already fired. This is a latent bug: if the 1st falls on a weekend/holiday, the monthly rebalance silently doesn't run that month at all.
- `_load_recent_history()` (`claude_manager.py:530`) loads the last 3 entries from `claude_rebalance_log.json` (`_LOG_PATH`, `claude_manager.py:29`) to give Claude month-over-month thesis continuity.
- Trade execution for Kimi Manager goes through `_execute_claude_sync` (`robinhood_client.py:449`), which handles `BUY`/`DOUBLE_DOWN`/`SELL`/`TRIM`/`HOLD`.
- Rebalance notifications go out via `notify_claude_manager_embed()` → `CLAUDE_MANAGER_WEBHOOK_URL` (Private Server `#manager`) and, for subscriber-facing callouts, `notify_claude_signal_feed()` → `CLAUDE_SUBSCRIBERS_WEBHOOK_URL` (KI Server) (`notifications.py:110,258`).

## New Behavior

### 1. Shared "first trading day of period" helper (fixes the #2 scheduling bug)

Extract the pattern already used ad hoc in `_quarterly_tax_report` into a shared helper, e.g. in `app/trading/alpaca_client.py` alongside `was_market_open_today()`:

```python
def is_first_trading_day_of(period_start: date) -> bool:
    """True if no trading day occurred between period_start and yesterday (inclusive)."""
    today = date.today()
    if period_start >= today:
        return True
    try:
        prior = get_client().get_calendar(
            GetCalendarRequest(start=period_start, end=today - timedelta(days=1))
        )
        return not prior
    except Exception as exc:
        log.warning("Could not verify first trading day for period starting %s: %s", period_start, exc)
        return True  # fail open — same behavior _quarterly_tax_report already relies on
```

Used by three call sites: the quarterly tax report (period = start of quarter month), the monthly rebalance (period = start of month), and the weekly inspection (period = start of week, plus a second call to detect the monthly-rebalance collision — see Section 3).

### 2. Fix `_claude_monthly_rebalance` scheduling

`scheduler.py:189-194` cron changes from `CronTrigger(day=1, ...)` to `CronTrigger(day="1-3", hour=9, minute=35, timezone=ET)`. `_claude_monthly_rebalance()` (`scheduler.py:153`) gains the same guard shape as `_quarterly_tax_report`:

```python
async def _claude_monthly_rebalance() -> None:
    if not was_market_open_today():
        log.info("_claude_monthly_rebalance: market holiday — skipping")
        return
    if not is_first_trading_day_of(date.today().replace(day=1)):
        log.info("_claude_monthly_rebalance: not first trading day of month — skipping")
        return
    from app.claude_manager import run_monthly_rebalance
    await run_monthly_rebalance()
```

This makes the rebalance reliably fire on whichever day 1-3 turns out to be the actual first trading day of the month, instead of silently skipping the month when the 1st is a weekend/holiday.

### 3. New weekly job: `_weekly_inspection`

New cron in `setup_jobs()` (`scheduler.py:163-200`): `CronTrigger(day_of_week="mon-wed", hour=9, minute=35, timezone=ET)` — same time-of-day as the rebalance (5 minutes after open), covering Monday-Wednesday so a holiday on Monday/Tuesday doesn't skip the week entirely.

```python
async def _weekly_inspection() -> None:
    if not was_market_open_today():
        log.info("_weekly_inspection: market holiday — skipping")
        return
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    if not is_first_trading_day_of(week_start):
        log.info("_weekly_inspection: not first trading day of week — skipping")
        return
    if is_first_trading_day_of(today.replace(day=1)):
        log.info("_weekly_inspection: coincides with monthly rebalance day — skipping")
        return
    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()
```

The `is_first_trading_day_of(today.replace(day=1))` call doubles as the monthly-rebalance-collision check: if today is also the first trading day of the month, the rebalance owns this week and Inspection stands down.

### 4. `run_weekly_inspection()` — new module `app/claude_inspection.py`

Mirrors the shape of `run_monthly_rebalance()` but scoped to holdings only:

1. Fetch current RH positions (reuse `rh_client.get_all_positions_async()`).
2. Enrich each holding with yfinance fundamentals/technicals (reuse `_fetch_yf_data` / `_fetch_technical_data` from `claude_manager.py`, run in parallel as today's code already does).
3. Load the **most recent thesis per ticker** — not the full 5-section research, just the prior verdict/conviction — from whichever of `claude_rebalance_log.json` or the new `claude_inspection_log.json` (Section 6) is most recent for that ticker.
4. Build a narrower prompt per holding: "here is what we believed as of `<date>` — has anything material happened in the last 7 days that changes this?" with a small web-search budget (2-3 searches/holding) rather than the full 30-search/80-turn research loop.
5. Prompt instructs a default-to-`HOLD` bias: only act on a specific, nameable trigger (earnings surprise, guidance change, major company-specific news, macro shock tied to the name, meaningful technical breakdown) — explicitly not on routine price noise. Same position-sizing constraints as the monthly rebalance apply (25% cap, no sector above 50%, resolved-bear-case required above 10% for any `DOUBLE_DOWN`).
6. Parse the response with the same `_parse_trade_block` (`claude_manager.py:510`) shape, restricted to `HOLD` / `SELL` / `TRIM` / `DOUBLE_DOWN`.
7. **Validation guard**: if the parsed trade block contains a `BUY` action, reject it and log an error rather than executing — this is a code-level constraint, not just a prompt instruction, since Inspection must never open a new position.
8. Execute via the existing `_execute_claude_sync`-style trade paths in `robinhood_client.py` — no new order-type code, since this runs after market open.
9. Log the run (Section 6) and notify Discord (Section 5).

### 5. Discord notifications — both channels

Send to both, mirroring the monthly rebalance's existing dual-channel pattern:

- **Private Server**: `notify_claude_manager_embed()` with a distinct header (`🔍 KIMI INSPECTION — WEEKLY CHECK`, vs. the monthly `🤖 KIMI PORTFOLIO MANAGER — MONTHLY REBALANCE`) so the two are visually distinguishable in `#manager`. Full detail: every holding reviewed, one-line "no material change" for untouched positions, full reasoning for anything actioned.
- **KI Server**: `notify_claude_signal_feed()` (plain-text, matching its existing usage — no new embed helper needed) with the same distinct header, summarizing only the holdings where action was taken (skip the "no change" boilerplate for the subscriber-facing message to avoid noise).

### 6. Logging — trade-source tagging

Inspection runs are written to a new, separate `claude_inspection_log.json` (same entry shape as `claude_rebalance_log.json`) rather than a `source` field on the shared log — this matches the existing per-purpose-file convention already used for `withdrawal_audit.json`, `pending_withdrawals.json`, etc., and keeps the two cadences/scopes (full rebalance vs. holdings-only check) cleanly separated on disk.

`_load_recent_history()` (`claude_manager.py:530`) is extended to also read the most recent `claude_inspection_log.json` entries per ticker, so:
- The monthly rebalance sees what Inspection already did to each holding that month, instead of re-deriving a thesis Inspection already updated.
- Inspection sees its own prior weeks' verdicts, not just the last full rebalance.

### 7. Confirmed, no code change needed

- **RH Session timing**: the keep-alive job runs 1-5 AM ET (`scheduler.py:177-182`); Inspection at 9:35 AM ET doesn't overlap it.
- **Snapshot integrity**: Inspection's own portfolio-value read is independent of and does not call or overwrite `record_rh_equity_snapshot()`, which stays exclusively the 4 PM ET P&L-report snapshot.

## Data Model Summary

| File | New/Modified | Purpose |
|---|---|---|
| `claude_inspection_log.json` | New | Audit log of every weekly inspection run — reviewed holdings, actions taken, reasoning |
| `claude_rebalance_log.json` | Unchanged schema | Continues to log monthly rebalance runs only |
| `app/claude_inspection.py` | New | `run_weekly_inspection()` and its helpers |
| `app/trading/alpaca_client.py` | Modified | New shared `is_first_trading_day_of()` helper |
| `app/scheduler.py` | Modified | New `_weekly_inspection` job; `_claude_monthly_rebalance` gains the holiday/first-trading-day guard; cron for the rebalance changes from `day=1` to `day="1-3"` |

## Error Handling

- If Inspection's Claude call returns a `BUY` action, reject it at the parsing/execution boundary (log an error, do not execute, do not silently downgrade it to something else) — this is the one hard invariant of the whole feature.
- If RH session is unavailable (`rh_client.available` is `False`), skip the run the same way `run_monthly_rebalance` already does, and post the same "session offline" warning embed pattern.
- If a given holding's enrichment (yfinance/technicals) fails, exclude it from that week's Inspection with a logged warning rather than failing the whole run — same graceful-degradation pattern already used for missing technical data in the monthly rebalance.

## Testing

- Unit test for `is_first_trading_day_of()` against a mocked calendar (holiday-adjacent month starts, holiday-adjacent week starts).
- Unit test confirming `_claude_monthly_rebalance` now fires when day 1 is a Saturday and day 3 is the actual first trading day (the bug this spec fixes).
- Unit test confirming `_weekly_inspection` skips when today is also the first trading day of the month.
- Unit test confirming a `BUY` action in Inspection's parsed trade block is rejected before reaching execution.
- Integration-style test: run `run_weekly_inspection()` against a fixture portfolio and confirm `claude_inspection_log.json` gets a well-formed entry and `_load_recent_history()` picks it up on a subsequent monthly rebalance call.
