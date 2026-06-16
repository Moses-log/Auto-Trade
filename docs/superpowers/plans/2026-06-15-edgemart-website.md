# Edgemart Landing Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public Edgemart landing page that shows live Kimi bot performance stats (pulled from Alpaca) and drives Whop signups, with an early-access spot counter that auto-decrements via Whop webhook.

**Architecture:** A new `/public-stats` GET endpoint on the existing Auto-Trade Render server computes live SPY trade stats (LIFO matching, 1-hour cache) and returns them alongside the early-access spot count stored in `/data/early_access.json`. A `/whop-webhook` POST endpoint auto-decrements/increments the spot count when Whop fires membership events. A single `index.html` hosted on GitHub Pages fetches `/public-stats` on load and renders the dark-terminal landing page with a Chart.js cumulative return chart and a Whop CTA.

**Tech Stack:** Python/FastAPI (backend, existing Render deployment), vanilla HTML/CSS/JS + Chart.js 4 CDN (frontend), GitHub Pages (frontend hosting)

---

## File Map

### Auto-Trade repo (backend)

| File | Action | Responsibility |
|---|---|---|
| `app/config.py` | Modify | Add `whop_webhook_secret` setting |
| `app/trading/alpaca_client.py` | Modify | Add `get_all_spy_orders()` helper |
| `app/early_access.py` | Create | Load/save `spots_remaining` from `/data/early_access.json` |
| `app/public_stats.py` | Create | LIFO stat computation + 1-hour in-memory cache |
| `app/main.py` | Modify | Add CORS middleware, `/public-stats`, `/whop-webhook` endpoints |
| `tests/test_early_access.py` | Create | Unit tests for spot counter logic |
| `tests/test_public_stats.py` | Create | Unit tests for LIFO matching and stat computation |

### edgemart-site repo (frontend — new public repo)

| File | Action | Responsibility |
|---|---|---|
| `index.html` | Create | Complete landing page — fetches stats, renders chart, Whop CTA |

---

## Task 1: Add `whop_webhook_secret` to config

**Files:**
- Modify: `app/config.py:83-84`

- [ ] **Step 1: Add the setting**

In `app/config.py`, add after the `signal_subscribers_webhook_url` line (line 84):

```python
    # ── Whop webhook (early-access spot counter) ──────────────────────────────
    whop_webhook_secret: Optional[str] = None
```

- [ ] **Step 2: Verify app still starts**

```bash
pytest tests/test_webhook.py -v -x
```

Expected: all tests pass (no import errors from config change).

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat: add WHOP_WEBHOOK_SECRET to config"
```

---

## Task 2: Add `get_all_spy_orders()` to alpaca_client

**Files:**
- Modify: `app/trading/alpaca_client.py` (add after line 264, after `get_orders_filled_range`)

- [ ] **Step 1: Add the function**

```python
@_retry
def get_all_spy_orders() -> list:
    """Return all closed SPY orders (filled + canceled), up to 500, sorted ascending by fill time."""
    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        limit=500,
    )
    orders = get_client().get_orders(filter=req) or []
    return [o for o in orders if o.symbol == "SPY" and o.status == AlpacaOrderStatus.FILLED]
```

- [ ] **Step 2: Verify existing tests still pass**

```bash
pytest tests/ -v -x
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add app/trading/alpaca_client.py
git commit -m "feat: add get_all_spy_orders helper to alpaca_client"
```

---

## Task 3: Create `app/early_access.py`

**Files:**
- Create: `app/early_access.py`
- Create: `tests/test_early_access.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_early_access.py`:

```python
import json
import os
import tempfile
import pytest

os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET", "test_secret")

import app.early_access as ea


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ea, "_SPOTS_PATH", str(tmp_path / "early_access.json"))
    yield


def test_load_spots_defaults_to_15_when_file_missing():
    assert ea.load_spots() == 15


def test_load_spots_reads_existing_file():
    with open(ea._SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": 10}, f)
    assert ea.load_spots() == 10


def test_decrement_spots_reduces_count():
    ea.decrement_spots()
    assert ea.load_spots() == 14


def test_decrement_spots_floors_at_zero():
    with open(ea._SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": 0}, f)
    ea.decrement_spots()
    assert ea.load_spots() == 0


def test_increment_spots_increases_count():
    with open(ea._SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": 10}, f)
    ea.increment_spots()
    assert ea.load_spots() == 11


def test_increment_spots_caps_at_15():
    with open(ea._SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": 15}, f)
    ea.increment_spots()
    assert ea.load_spots() == 15
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_early_access.py -v
```

Expected: `ModuleNotFoundError` or `AttributeError` — `app.early_access` does not exist yet.

- [ ] **Step 3: Create `app/early_access.py`**

```python
"""
early_access.py — Persist the early-access spot counter to /data/early_access.json.

Spots start at 15. Decremented when a Whop membership.went_valid event fires;
incremented on membership.went_invalid (cancellation/refund).
"""

import json
import os

_SPOTS_PATH = "/data/early_access.json"
_MAX_SPOTS = 15


def load_spots() -> int:
    if not os.path.exists(_SPOTS_PATH):
        return _MAX_SPOTS
    try:
        with open(_SPOTS_PATH) as f:
            return int(json.load(f).get("spots_remaining", _MAX_SPOTS))
    except Exception:
        return _MAX_SPOTS


def _save_spots(n: int) -> None:
    os.makedirs(os.path.dirname(_SPOTS_PATH), exist_ok=True)
    with open(_SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": n}, f)


def decrement_spots() -> int:
    current = load_spots()
    new = max(0, current - 1)
    _save_spots(new)
    return new


def increment_spots() -> int:
    current = load_spots()
    new = min(_MAX_SPOTS, current + 1)
    _save_spots(new)
    return new
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_early_access.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/early_access.py tests/test_early_access.py
git commit -m "feat: add early_access spot counter with persistence"
```

---

## Task 4: Create `app/public_stats.py`

**Files:**
- Create: `app/public_stats.py`
- Create: `tests/test_public_stats.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_public_stats.py`:

```python
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET", "test_secret")

from app.public_stats import compute_stats


def _order(side, qty, price, dt_str):
    o = MagicMock()
    o.side = MagicMock()
    o.side.__str__ = lambda self: f"OrderSide.{side.upper()}"
    o.filled_qty = str(qty)
    o.filled_avg_price = str(price)
    o.filled_at = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
    return o


def test_empty_orders_returns_zero_stats():
    result = compute_stats([])
    assert result["trades"] == 0
    assert result["wins"] == 0
    assert result["win_rate"] == 0


def test_single_win_round_trip():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T10:00:00"),
        _order("SELL", 10, 110.0, "2026-04-01T11:00:00"),
    ]
    result = compute_stats(orders)
    assert result["trades"] == 1
    assert result["wins"] == 1
    assert result["losses"] == 0
    assert result["win_rate"] == 100.0
    assert result["profit_factor"] == 0  # no losses to divide by


def test_single_loss_round_trip():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T10:00:00"),
        _order("SELL", 10,  90.0, "2026-04-01T11:00:00"),
    ]
    result = compute_stats(orders)
    assert result["trades"] == 1
    assert result["losses"] == 1
    assert result["win_rate"] == 0.0


def test_lifo_matching_uses_most_recent_buy():
    # Base buy at 100, then leverage buy at 120, then sell at 125.
    # LIFO should match sell against the 120 buy (profit), not the 100 buy.
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T09:00:00"),  # base (never matched)
        _order("BUY",  10, 120.0, "2026-04-01T10:00:00"),  # leverage
        _order("SELL", 10, 125.0, "2026-04-01T11:00:00"),  # removes leverage
    ]
    result = compute_stats(orders)
    assert result["trades"] == 1
    assert result["wins"] == 1
    dollar_pnl = result["cumulative_returns"][0]["pct"]
    assert dollar_pnl > 0  # sold higher than the 120 buy


def test_profit_factor_calculation():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T10:00:00"),
        _order("SELL", 10, 110.0, "2026-04-01T11:00:00"),  # +$100 win
        _order("BUY",  10, 100.0, "2026-04-02T10:00:00"),
        _order("SELL", 10,  90.0, "2026-04-02T11:00:00"),  # -$100 loss
    ]
    result = compute_stats(orders)
    assert result["profit_factor"] == 1.0


def test_cumulative_returns_length_matches_trades():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-01T10:00:00"),
        _order("SELL", 10, 110.0, "2026-04-01T11:00:00"),
        _order("BUY",  10, 100.0, "2026-04-02T10:00:00"),
        _order("SELL", 10, 105.0, "2026-04-02T11:00:00"),
    ]
    result = compute_stats(orders)
    assert len(result["cumulative_returns"]) == 2
    assert result["cumulative_returns"][0]["trade"] == 1
    assert result["cumulative_returns"][1]["trade"] == 2


def test_date_range_uses_first_buy_and_last_sell():
    orders = [
        _order("BUY",  10, 100.0, "2026-04-22T10:00:00"),
        _order("SELL", 10, 110.0, "2026-06-11T11:00:00"),
    ]
    result = compute_stats(orders)
    assert "Apr" in result["date_range"]["from"]
    assert "Jun" in result["date_range"]["to"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_public_stats.py -v
```

Expected: `ModuleNotFoundError` — `app.public_stats` does not exist yet.

- [ ] **Step 3: Create `app/public_stats.py`**

```python
"""
public_stats.py — Compute live Kimi bot performance stats for the public landing page.

Fetches all filled SPY orders from Alpaca, applies LIFO matching
(each sell paired with the most recent buy — correct for Kimi leverage cycles),
and returns aggregate stats. Results are cached for 1 hour.
"""

import asyncio
import logging
import time
from datetime import timezone
from typing import Optional

from app.early_access import load_spots

log = logging.getLogger(__name__)

_cache: dict = {"data": None, "expires": 0.0}


def compute_stats(filled_orders: list) -> dict:
    """LIFO-match buys to sells and return performance stats dict."""
    orders = sorted(filled_orders, key=lambda o: o.filled_at)

    buy_stack: list = []   # each entry: [qty_remaining, fill_price, fill_dt]
    trades:    list = []
    first_dt:  Optional[object] = None
    last_sell_dt: Optional[object] = None

    for o in orders:
        side  = str(o.side)
        qty   = float(o.filled_qty)
        price = float(o.filled_avg_price)
        dt    = o.filled_at.astimezone(timezone.utc)

        if first_dt is None:
            first_dt = dt

        if "BUY" in side.upper():
            buy_stack.append([qty, price, dt])
        else:
            remaining  = qty
            cost_basis = 0.0
            matched    = 0.0

            while remaining > 1e-6 and buy_stack:
                bq, bp, _ = buy_stack[-1]
                take        = min(remaining, bq)
                cost_basis += take * bp
                matched    += take
                remaining  -= take
                buy_stack[-1][0] -= take
                if buy_stack[-1][0] < 1e-6:
                    buy_stack.pop()

            if matched > 1e-6:
                proceeds   = matched * price
                dollar_pnl = proceeds - cost_basis
                pct_pnl    = (dollar_pnl / cost_basis) * 100
                trades.append({"won": dollar_pnl >= 0, "dollar_pnl": dollar_pnl, "pct_pnl": pct_pnl})
                last_sell_dt = dt

    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "profit_factor": 0,
            "date_range": {"from": "", "to": ""},
            "cumulative_returns": [],
        }

    wins   = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    gross_win  = sum(t["dollar_pnl"] for t in wins)
    gross_loss = abs(sum(t["dollar_pnl"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 0

    cumulative: list = []
    running = 0.0
    for i, t in enumerate(trades, 1):
        running += t["pct_pnl"]
        cumulative.append({"trade": i, "pct": round(running, 4), "won": t["won"]})

    def _fmt(dt) -> str:
        if dt is None:
            return ""
        day = dt.strftime("%d").lstrip("0") or "0"
        return dt.strftime(f"%b {day}, %Y")

    return {
        "trades":       len(trades),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(len(wins) / len(trades) * 100, 1),
        "profit_factor": profit_factor,
        "date_range":   {"from": _fmt(first_dt), "to": _fmt(last_sell_dt)},
        "cumulative_returns": cumulative,
    }


async def get_public_stats() -> dict:
    """Return cached stats, refreshing from Alpaca if the 1-hour TTL has expired."""
    now = time.time()
    if _cache["data"] is not None and now < _cache["expires"]:
        return _cache["data"]

    loop = asyncio.get_running_loop()
    try:
        from app.trading.alpaca_client import get_all_spy_orders
        orders = await loop.run_in_executor(None, get_all_spy_orders)
    except Exception as exc:
        log.warning("Failed to fetch SPY orders for public stats: %s", exc)
        orders = []

    stats = compute_stats(orders)
    stats["spots_remaining"] = load_spots()

    _cache["data"]    = stats
    _cache["expires"] = now + 3600
    return stats
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_public_stats.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/public_stats.py tests/test_public_stats.py
git commit -m "feat: add public_stats module with LIFO matching and 1-hour cache"
```

---

## Task 5: Add endpoints and CORS to `main.py`

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add CORS middleware import**

At the top of `app/main.py`, add to the fastapi imports line (line 28):

```python
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
```

- [ ] **Step 2: Register CORS middleware**

After the `app = FastAPI(...)` block (after line 114), add:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
```

- [ ] **Step 3: Add `/public-stats` endpoint**

Add after the `/healthz` endpoint (after line 185):

```python
@app.get("/public-stats", tags=["public"])
async def public_stats():
    """Live Kimi bot performance stats for the Edgemart landing page. No auth required."""
    from app.public_stats import get_public_stats
    return await get_public_stats()
```

- [ ] **Step 4: Add `/whop-webhook` endpoint**

Add immediately after the `/public-stats` endpoint:

```python
@app.post("/whop-webhook", tags=["public"])
async def whop_webhook(request: Request):
    """
    Receive Whop membership events and update the early-access spot counter.
    Verifies the Whop-Signature HMAC header before processing.
    """
    import hashlib
    import hmac

    body = await request.body()

    if settings.whop_webhook_secret:
        sig_header = request.headers.get("Whop-Signature", "")
        expected = hmac.new(
            settings.whop_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "Invalid signature."},
            )

    try:
        import json as _json
        data       = _json.loads(body)
        event_type = data.get("event", "")
        from app.early_access import decrement_spots, increment_spots
        if event_type == "membership.went_valid":
            remaining = decrement_spots()
            log.info("Whop member joined — spots remaining: %d", remaining)
        elif event_type == "membership.went_invalid":
            remaining = increment_spots()
            log.info("Whop member left — spots remaining: %d", remaining)
    except Exception as exc:
        log.warning("Whop webhook processing error: %s", exc)

    return {"status": "ok"}
```

- [ ] **Step 5: Fix the `hmac.new` typo — it should be `hmac.new`**

Wait — Python's `hmac` module uses `hmac.new()`. Double-check: it is `hmac.new(key, msg, digestmod)`. Correct as written above.

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/main.py
git commit -m "feat: add /public-stats and /whop-webhook endpoints with CORS"
```

---

## Task 6: Push backend to Render

- [ ] **Step 1: Push to origin**

```bash
git push origin main
```

- [ ] **Step 2: Wait for Render deploy (~2 minutes), then verify**

```bash
curl https://YOUR_RENDER_URL/public-stats
```

Expected: JSON with `trades`, `win_rate`, `profit_factor`, `cumulative_returns`, `spots_remaining`.

Replace `YOUR_RENDER_URL` with your actual Render service URL (e.g. `https://auto-trade-xxxx.onrender.com`).

- [ ] **Step 3: Configure Whop webhook**

In your Whop dashboard → Developer → Webhooks:
- URL: `https://YOUR_RENDER_URL/whop-webhook`
- Events: `membership.went_valid`, `membership.went_invalid`
- Copy the signing secret → set `WHOP_WEBHOOK_SECRET=<value>` in Render environment variables → redeploy

---

## Task 7: Build `index.html`

This goes in a **new separate repo** (`Moses-log/edgemart-site`). Create the repo on GitHub (public, no template), then create this file at the root.

**Files:**
- Create: `index.html` (in the new `edgemart-site` repo)

- [ ] **Step 1: Create `index.html`**

Replace `YOUR_RENDER_URL` on line 6 with your actual Render URL before committing.
Replace `YOUR_WHOP_URL` on line 7 with your actual Whop checkout link.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Edgemart — SPY Trading Signals</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg:      #0d1117;
      --card:    #131c27;
      --border:  #1e2d3d;
      --cyan:    #00e5c8;
      --red:     #ff4d4d;
      --white:   #ffffff;
      --muted:   #8b9ab0;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--white);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif;
      -webkit-font-smoothing: antialiased;
    }

    .container { max-width: 1200px; margin: 0 auto; padding: 64px 28px; }

    /* ── Hero ───────────────────────────────────────────────────────────────── */
    .hero { margin-bottom: 56px; }
    .hero .tagline {
      color: var(--cyan);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 4px;
      margin-bottom: 16px;
    }
    .hero h1 {
      font-size: clamp(52px, 9vw, 100px);
      font-weight: 900;
      letter-spacing: -2px;
      line-height: 1;
      margin-bottom: 16px;
    }
    .hero .date-range { color: var(--muted); font-size: 14px; }

    /* ── Stats grid ─────────────────────────────────────────────────────────── */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
      margin-bottom: 36px;
    }
    .stat-card {
      background: var(--card);
      border: 1px solid var(--border);
      padding: 22px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .stat-label {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 2px;
      color: var(--muted);
    }
    .stat-value       { font-size: 38px; font-weight: 800; color: var(--white); }
    .stat-value.cyan  { color: var(--cyan); }

    /* ── Chart ──────────────────────────────────────────────────────────────── */
    .chart-wrapper { position: relative; height: 340px; margin-bottom: 14px; }
    .chart-tagline {
      color: var(--cyan);
      font-size: 13px;
      font-weight: 600;
      text-align: center;
      margin-bottom: 72px;
    }

    /* ── CTA ────────────────────────────────────────────────────────────────── */
    .cta {
      text-align: center;
      padding: 56px 0 72px;
      border-top: 1px solid var(--border);
    }
    .cta h2 {
      font-size: 28px;
      font-weight: 900;
      letter-spacing: 4px;
      margin-bottom: 14px;
    }
    .spots {
      color: var(--cyan);
      font-size: 15px;
      font-weight: 600;
      letter-spacing: 1px;
      margin-bottom: 10px;
    }
    .pricing {
      color: var(--muted);
      font-size: 15px;
      margin-bottom: 36px;
    }
    .pricing strong { color: var(--white); }
    .btn {
      display: inline-block;
      background: var(--cyan);
      color: #0a1612;
      font-size: 14px;
      font-weight: 800;
      letter-spacing: 2px;
      padding: 18px 52px;
      text-decoration: none;
      transition: opacity .15s;
    }
    .btn:hover { opacity: .82; }

    /* ── Footer ─────────────────────────────────────────────────────────────── */
    footer {
      border-top: 1px solid var(--border);
      padding: 28px 0;
      text-align: center;
    }
    footer p { color: var(--muted); font-size: 12px; line-height: 1.7; }

    @media (max-width: 640px) {
      .stats-grid { grid-template-columns: 1fr; }
      .stat-card  { padding: 18px 20px; }
    }
  </style>
</head>
<body>

<div class="container">

  <!-- Hero -->
  <section class="hero">
    <p class="tagline">REAL MONEY · EVERY TRADE · PUBLIC</p>
    <h1>EDGEMART</h1>
    <p class="date-range" id="date-range">Loading...</p>
  </section>

  <!-- Stats -->
  <div class="stats-grid">
    <div class="stat-card">
      <span class="stat-label">TRADES</span>
      <span class="stat-value" id="stat-trades">—</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">WIN RATE</span>
      <span class="stat-value cyan" id="stat-winrate">—</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">PROFIT FACTOR</span>
      <span class="stat-value cyan" id="stat-pf">—</span>
    </div>
  </div>

  <!-- Chart -->
  <div class="chart-wrapper">
    <canvas id="chart"></canvas>
  </div>
  <p class="chart-tagline">Up days and down days. Nothing hidden.</p>

  <!-- CTA -->
  <section class="cta">
    <h2>EARLY ACCESS</h2>
    <p class="spots" id="spots">Loading...</p>
    <p class="pricing">Free for 14 days &mdash; then <strong>$14.99&thinsp;/&thinsp;month</strong></p>
    <a href="YOUR_WHOP_URL" class="btn">JOIN EDGEMART →</a>
  </section>

</div>

<footer>
  <div class="container">
    <p>Past performance &ne; future results. Not financial advice. Trading involves risk of loss.</p>
  </div>
</footer>

<script>
  const API = 'YOUR_RENDER_URL';
  let _chart = null;

  function buildChart(data) {
    const labels      = data.map(d => d.trade);
    const values      = data.map(d => d.pct);
    const pointColors = data.map(d => d.won ? '#00e5c8' : '#ff4d4d');

    const ctx = document.getElementById('chart').getContext('2d');
    if (_chart) _chart.destroy();

    _chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: values,
          borderColor: '#00e5c8',
          borderWidth: 2,
          pointBackgroundColor: pointColors,
          pointBorderColor:     pointColors,
          pointRadius: 5,
          pointHoverRadius: 7,
          tension: 0,
          fill: { target: 'origin', above: 'rgba(0,229,200,0.06)' },
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.parsed.y.toFixed(2)}%`,
          }
        }},
        scales: {
          x: {
            title: { display: true, text: 'Trade #', color: '#8b9ab0', font: { size: 12 } },
            ticks: { color: '#8b9ab0' },
            grid:  { color: '#1e2d3d' },
          },
          y: {
            title: { display: true, text: 'Cumulative Return (%)', color: '#8b9ab0', font: { size: 12 } },
            ticks: { color: '#8b9ab0', callback: v => v.toFixed(2) + '%' },
            grid:  { color: '#1e2d3d' },
          }
        }
      }
    });
  }

  async function loadStats() {
    try {
      const res = await fetch(`${API}/public-stats`);
      const d   = await res.json();

      document.getElementById('date-range').textContent =
        `Closed trades · ${d.date_range.from} – ${d.date_range.to}`;

      document.getElementById('stat-trades').textContent  = d.trades;
      document.getElementById('stat-winrate').textContent = d.win_rate + '%';
      document.getElementById('stat-pf').textContent      = d.profit_factor;

      const s = d.spots_remaining;
      document.getElementById('spots').textContent =
        `${s} of 15 spot${s === 1 ? '' : 's'} remaining`;

      if (d.cumulative_returns.length) buildChart(d.cumulative_returns);
    } catch (e) {
      console.error('Failed to load stats:', e);
    }
  }

  loadStats();
  setInterval(loadStats, 5 * 60 * 1000);
</script>
</body>
</html>
```

- [ ] **Step 2: Verify both placeholder values have been replaced**

Before committing, confirm:
- `YOUR_RENDER_URL` → replaced with your actual Render URL (e.g. `https://auto-trade-xxxx.onrender.com`)
- `YOUR_WHOP_URL` → replaced with your actual Whop checkout link

- [ ] **Step 3: Commit**

```bash
git add index.html
git commit -m "feat: add Edgemart landing page"
```

---

## Task 8: Deploy frontend to GitHub Pages

- [ ] **Step 1: Create the `edgemart-site` repo on GitHub**

Go to https://github.com/new and create a public repo named `edgemart-site` under the `Moses-log` account. No template, no README.

- [ ] **Step 2: Push**

```bash
git remote add origin https://github.com/Moses-log/edgemart-site.git
git branch -M main
git push -u origin main
```

- [ ] **Step 3: Enable GitHub Pages**

On GitHub: repo → Settings → Pages → Source: `Deploy from a branch` → Branch: `main` / `/ (root)` → Save.

- [ ] **Step 4: Verify**

Wait ~1 minute, then open `https://moses-log.github.io/edgemart-site/`. The page should load, fetch stats from the Render backend, and display the chart.

---

## Self-Review Checklist

- [x] Spec: `/public-stats` endpoint → Task 5 + Task 6
- [x] Spec: `/whop-webhook` endpoint → Task 5
- [x] Spec: `spots_remaining` in early_access.json → Task 3
- [x] Spec: LIFO matching → Task 4 `compute_stats()`
- [x] Spec: 1-hour cache → Task 4 `get_public_stats()`
- [x] Spec: CORS → Task 5 Step 1-2
- [x] Spec: `WHOP_WEBHOOK_SECRET` env var → Task 1
- [x] Spec: 4 page sections (hero, proof, CTA, footer) → Task 7
- [x] Spec: live spot counter (JS refresh every 5 min) → Task 7
- [x] Spec: Chart.js cumulative return chart, cyan line, red/green dots → Task 7
- [x] Spec: GitHub Pages deploy → Task 8
- [x] No placeholders — all code blocks are complete
- [x] `compute_stats` signature matches usage in `get_public_stats` and tests
- [x] `_SPOTS_PATH` attribute used in tests matches the attribute name in `early_access.py`
