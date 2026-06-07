# Investor Tracking Feature — Design Spec
**Date:** 2026-05-09
**Project:** Moses-log/Auto-Trade

---

## Overview

The Auto-Trade system manages a shared Alpaca portfolio acting as a hedge fund with multiple investors. This feature adds per-investor equity tracking so each person can see how their stake grows or shrinks as the portfolio (indexed to SPY) moves daily.

---

## Initial Investor State

| Investor | Deposit | Entry SPY Price |
|----------|---------|-----------------|
| Moses    | $300.00 | $707.116        |
| David    | $2,000.00 | $710.6993     |
| Gabe     | $3,000.00 | $710.36       |
| **Total** | **$5,300.00** | **$710.305 (weighted avg)** |

---

## Data Model

A file `investors.json` lives at the repo root (same level as `app/`). It is committed to git and is the source of truth for all investor records.

```json
{
  "investors": [
    {
      "name": "Moses",
      "deposits": [
        {"amount": 300, "entry_spy": 707.116, "date": "2026-05-09"}
      ]
    },
    {
      "name": "David",
      "deposits": [
        {"amount": 2000, "entry_spy": 710.6993, "date": "2026-05-09"}
      ]
    },
    {
      "name": "Gabe",
      "deposits": [
        {"amount": 3000, "entry_spy": 710.36, "date": "2026-05-09"}
      ]
    }
  ]
}
```

### Rules
- Deposits are **append-only** — existing entries are never modified or deleted.
- Each deposit record has three fields: `amount` (USD), `entry_spy` (SPY price at time of deposit), `date` (ISO 8601).
- A new investor joining is handled by creating a new entry with their first deposit.
- The file must be committed to git after every deposit so Render redeploys don't revert the state.
- If `investors.json` does not exist at runtime, investor reporting is skipped and a warning is logged — no crash, no interruption to trading or P&L reports.
- Investor names are stored with original casing as written in the JSON. Lookups (e.g. `/deposit` endpoint) match case-insensitively by normalizing both sides to lowercase.

---

## New Module: `app/investors.py`

Owns all investor data loading, equity calculation, and Discord message formatting. No other module should duplicate this logic.

### Equity Calculation

For each investor, every deposit is treated independently:

```
current_equity  = sum( d.amount × (current_SPY / d.entry_spy)  for d in deposits )
total_deposited = sum( d.amount for d in deposits )
dollar_pnl      = current_equity − total_deposited
pct_pnl         = dollar_pnl / total_deposited × 100
portfolio_share = investor_equity / total_portfolio_equity × 100
```

`total_portfolio_equity` is the sum of all investors' `current_equity` — not the raw Alpaca account balance. This keeps the math self-consistent regardless of trading P&L from signals.

### Public Interface

```python
load_investors() -> list[Investor]
save_investors(investors: list[Investor]) -> None
compute_breakdown(investors: list[Investor], spy_price: float) -> InvestorBreakdown
format_discord_message(breakdown: InvestorBreakdown, date: str) -> str
```

---

## Discord Report

### Channel Routing

| Env Var | Purpose |
|---------|---------|
| `DISCORD_WEBHOOK_URL` | Existing — trade alerts and P&L reports |
| `DISCORD_INVESTORS_WEBHOOK_URL` | New — investor breakdown messages |

If `DISCORD_INVESTORS_WEBHOOK_URL` is not set, the investor breakdown falls back to `DISCORD_WEBHOOK_URL`. If neither is set, the message is skipped and a warning is logged.

### Message Format

```
📊 **Investor Breakdown — May 9, 2026**
SPY: $563.20

**Moses**
> Deposited: $300.00 | Entry SPY: $707.12
> Current Equity: $238.73
> P&L: -$61.27 (-20.45%)
> Portfolio Share: 4.5%

**David**
> Deposited: $2,000.00 | Entry SPY: $710.70
> Current Equity: $1,589.18
> P&L: -$410.82 (-20.54%)
> Portfolio Share: 37.7%

**Gabe**
> Deposited: $3,000.00 | Entry SPY: $710.36
> Current Equity: $2,383.77
> P&L: -$616.23 (-20.54%)
> Portfolio Share: 56.5%

─────────────────────────
**Total Portfolio: $4,211.68**
**Total Deposited: $5,300.00**
**Overall P&L: -$1,088.32 (-20.53%)**
```

### Scheduler Timing

| Day | 4:00 PM ET | 4:01 PM ET | 4:02 PM ET | 4:03 PM ET |
|-----|-----------|-----------|-----------|-----------|
| Mon–Thu | Daily P&L | — | Investor Breakdown | — |
| Friday | Daily P&L | Weekly P&L | — | Investor Breakdown |

---

## `/deposit` Endpoint

**Method:** `POST /deposit`

**Request payload:**
```json
{
  "secret": "your-webhook-secret",
  "investor": "Moses",
  "amount": 500,
  "spy_price": null
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `secret` | Yes | Same shared secret used by `/webhook` |
| `investor` | Yes | Investor name (case-insensitive match). If not found, a new investor entry is created. |
| `amount` | Yes | Deposit amount in USD (must be positive) |
| `spy_price` | No | SPY price at time of deposit. If `null`, fetches current price from Alpaca automatically. |

**On success:** Appends the deposit to `investors.json`, saves the file, returns the investor's full updated deposit history.

**On failure:** Returns appropriate HTTP error. Does not modify `investors.json`.

**Important:** After using the endpoint, commit the updated `investors.json` to git so the state persists through Render redeploys. Manual JSON editing is always a valid fallback.

---

## Config Changes

Add to `app/config.py`:
```python
discord_investors_webhook_url: Optional[str] = None
```

---

## Files Changed / Created

| File | Change |
|------|--------|
| `investors.json` | New — repo root, initial investor data |
| `app/investors.py` | New — load/save, equity math, Discord formatting |
| `app/config.py` | Add `discord_investors_webhook_url` optional field |
| `app/models.py` | Add `DepositRequest` Pydantic model |
| `app/notifications.py` | Add `notify_investors()` that routes to correct webhook |
| `app/pnl.py` | Add `send_investor_report()` function |
| `app/scheduler.py` | Register `investor_breakdown` cron jobs |
| `app/main.py` | Register `POST /deposit` endpoint |
