# Portfolio Equity Chart — Design Spec

**Date:** 2026-05-16
**Feature:** Attach a portfolio vs SPY equity curve chart to weekly, monthly, yearly, and all-time P&L Discord reports

---

## Problem

P&L reports are text-only. A visual equity curve showing portfolio vs SPY % return over the same period gives immediate intuition about performance that raw numbers don't.

---

## Goal

Attach a PNG chart to weekly, monthly, yearly, and all-time Discord P&L messages. Chart shows portfolio % return and SPY % return from the start of the reporting period, normalized to the same 0% baseline.

---

## Scope

**Reports that get charts:** weekly, monthly, yearly, all-time
**Reports that stay text-only:** daily, YTD (periods too short/variable for a meaningful curve right now)

---

## Architecture

```
report function
      │
      ├── get_portfolio_history()     → equity[], timestamp[]
      ├── fetch_spy_history(start, end) → SPY DataFrame
      ├── compute_spy_pct()           → float (unchanged)
      │
      ├── generate_equity_chart(equity, timestamps, spy_df, title) → bytes
      │         ├── Normalize both to % return from period start
      │         ├── Plot portfolio (green) + SPY (orange dashed)
      │         └── Return PNG as BytesIO bytes
      │
      ├── _format_message()           → text (unchanged)
      │
      └── notify_with_chart(msg, chart_bytes)
                ├── POST multipart to DISCORD_WEBHOOK_URL
                │     payload_json: {"content": msg}
                │     file: chart.png
                └── Fallback to notify(msg) if chart_bytes is None
```

---

## New Files

### `app/chart.py`

Single public function:

```python
def generate_equity_chart(
    equity: list[float],
    timestamps: list[int],
    spy_df,              # yfinance DataFrame with "Close" column
    title: str,
) -> bytes:
```

- Converts Unix timestamps to dates
- Normalizes portfolio equity to % return: `(eq - eq[0]) / eq[0] * 100`
- Normalizes SPY close prices to % return from first available price
- Plots both on the same axes
  - Portfolio: solid green line, label shows final % (e.g. `Portfolio +8.75%`)
  - SPY: dashed orange line, label shows final % (e.g. `S&P 500 +1.20%`)
- X-axis: dates (auto-formatted by matplotlib)
- Y-axis: % return with `%` suffix
- Horizontal dashed grey line at 0%
- Title from caller (e.g. `"Weekly Performance: May 9–16, 2026"`)
- Returns PNG bytes via `BytesIO` — nothing written to disk
- Raises on failure (caller catches and falls back)

---

## Changes to Existing Files

### `app/pnl.py`

**New helper:** `fetch_spy_history(start_date: date, end_date: date)`

```python
def fetch_spy_history(start_date, end_date):
    """Return yfinance SPY DataFrame for the given date range. Returns None on failure."""
```

Uses `yf.Ticker("SPY").history(start=start_date, end=end_date)`. Returns `None` on exception. Used by report functions to get the full price series for chart generation.

**Updated report functions:** `send_weekly_report`, `send_monthly_report`, `send_yearly_report`, `send_alltime_report`

Each adds after computing `result` and `spy_pct`:

```python
chart_bytes = None
try:
    spy_df = fetch_spy_history(period_start_date, date.today())
    if spy_df is not None:
        chart_bytes = generate_equity_chart(
            history.equity, history.timestamp, spy_df, chart_title
        )
except Exception as exc:
    log.warning("Chart generation failed: %s", exc)

if chart_bytes:
    await notify_with_chart(msg, chart_bytes)
else:
    await notify(msg)
```

Period start dates:
- **Weekly:** `now.date() - timedelta(days=now.weekday())` (Monday)
- **Monthly:** `now.date().replace(day=1)` (1st of month)
- **Yearly:** `now.date().replace(month=1, day=1)` (Jan 1) — same window as the Alpaca 1A period
- **All-time:** `start_dt.date()` (already computed from first non-zero equity timestamp)

### `app/notifications.py`

**New function:** `notify_with_chart(message: str, chart_bytes: bytes) -> None`

Posts to `DISCORD_WEBHOOK_URL` as multipart/form-data:
- `payload_json`: `{"content": message[:2000]}`
- `file`: `("chart.png", chart_bytes, "image/png")`

Falls back to `notify(message)` if `DISCORD_WEBHOOK_URL` is not set or if the POST fails.

### `requirements.txt`

Add: `matplotlib>=3.8.0`

---

## Error Handling

| Failure | Behaviour |
|---|---|
| `fetch_spy_history` returns None | Chart skipped, text-only report sent |
| `generate_equity_chart` raises | Warning logged, text-only report sent |
| `notify_with_chart` POST fails | Warning logged (same as existing notify failures) |
| SPY data doesn't cover full period | Chart still generated with available SPY data |

Chart failures never block the text report from sending.

---

## New/Modified Files

| File | Action |
|---|---|
| `app/chart.py` | Create — chart generation |
| `app/pnl.py` | Modify — add `fetch_spy_history`, update 4 report functions |
| `app/notifications.py` | Modify — add `notify_with_chart` |
| `requirements.txt` | Modify — add matplotlib |
| `tests/test_chart.py` | Create — chart generation tests |
| `tests/test_pnl.py` | Modify — update 4 report tests to mock chart |

---

## Out of Scope

- Charts for daily or YTD reports
- Interactive charts (Discord only supports static images)
- Saving charts to disk or GitHub
- Chart customisation options (colors, size, style)
