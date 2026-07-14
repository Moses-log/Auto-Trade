# Code-Enforced Risk Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce Kimi's risk policy in code — hard-clamp any single position to ≤25% (BUY/DOUBLE_DOWN/TRIM) and post a Discord alert (no auto-scale) when post-trade sector exposure would exceed 50% — in both the monthly rebalance and the weekly inspection.

**Architecture:** A new pure module `app/risk_guardrails.py` (clamp, sector exposure, sector resolution, embed) imported by `run_monthly_rebalance` (`claude_manager.py`) and `run_weekly_inspection` (`claude_inspection.py`). The clamp mutates `target_weight_pct` in the parsed trade dicts before the execution loops read them, so the sizing math is untouched.

**Tech Stack:** Python 3.10+ (Render runtime), yfinance, pytest. No new dependencies.

## Global Constraints

- `MAX_POSITION_PCT = 25.0`, `MAX_SECTOR_PCT = 50.0`; `_CLAMPED_ACTIONS = ("BUY", "DOUBLE_DOWN", "TRIM")` — verbatim.
- Clamp only lowers `target_weight_pct` in place for the clamped actions; SELL/HOLD and trades missing `target_weight_pct` are untouched. Clamp never rejects a trade.
- Sector is **alert-only** — never auto-scaled. Only sectors strictly `> 50%` warn; the `Unknown` bucket never warns.
- No import cycle: `risk_guardrails.py` must NOT import `claude_manager` or `pnl` at module top — lazy imports only, inside `format_guardrail_embed` (`claude_manager`) and `_yf_sector_fetch` (`pnl`).
- The clamp is safety-critical and runs first/unconditionally; the sector check is best-effort and wrapped so it can never break a run.
- RUN TESTS WITH THIS EXACT INTERPRETER (default `python` lacks pytest):
  `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest`
- Full-suite baseline: exactly 8 pre-existing failures in `tests/test_pnl.py` and `tests/test_trade_notifier.py`; any other failure is a regression.

---

### Task 1: Module + `clamp_position_weights`

**Files:**
- Create: `app/risk_guardrails.py`
- Test: `tests/test_risk_guardrails.py` (create)

**Interfaces:**
- Produces: constants `MAX_POSITION_PCT=25.0`, `MAX_SECTOR_PCT=50.0`, `_CLAMPED_ACTIONS`; `ClampEvent(ticker, original_pct, clamped_pct)`; `clamp_position_weights(trades: list[dict]) -> list[ClampEvent]` (mutates in place).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk_guardrails.py
import os
os.environ.setdefault("ALPACA_API_KEY", "t")
os.environ.setdefault("ALPACA_SECRET_KEY", "t")
os.environ.setdefault("WEBHOOK_SECRET", "s")

from app.risk_guardrails import clamp_position_weights, ClampEvent, MAX_POSITION_PCT


def test_clamps_oversized_buy_and_double_down_and_trim():
    trades = [
        {"action": "BUY", "ticker": "nvda", "target_weight_pct": 32},
        {"action": "DOUBLE_DOWN", "ticker": "META", "target_weight_pct": 40},
        {"action": "TRIM", "ticker": "AMD", "target_weight_pct": 30},
    ]
    events = clamp_position_weights(trades)
    assert all(t["target_weight_pct"] == MAX_POSITION_PCT for t in trades)
    assert {e.ticker for e in events} == {"NVDA", "META", "AMD"}
    assert events[0].original_pct == 32 and events[0].clamped_pct == 25.0


def test_leaves_within_cap_and_sell_hold_untouched():
    trades = [
        {"action": "BUY", "ticker": "MSFT", "target_weight_pct": 20},
        {"action": "SELL", "ticker": "NOW"},
        {"action": "HOLD", "ticker": "GOOG", "target_weight_pct": 30},   # HOLD not clamped
        {"action": "TRIM", "ticker": "AAPL"},                            # missing pct: no error
    ]
    events = clamp_position_weights(trades)
    assert events == []
    assert trades[0]["target_weight_pct"] == 20
    assert trades[2]["target_weight_pct"] == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_risk_guardrails.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.risk_guardrails'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/risk_guardrails.py`:

```python
"""risk_guardrails.py — Code-enforced position/sector risk limits.

Hard-clamps any single position to <= MAX_POSITION_PCT and computes post-trade
sector exposure for alerting (no auto-scale). Pure except the yfinance sector
adapter. claude_manager / pnl imports are lazy (inside functions) to avoid a
cycle, since claude_manager imports this module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_POSITION_PCT = 25.0
MAX_SECTOR_PCT = 50.0
_CLAMPED_ACTIONS = ("BUY", "DOUBLE_DOWN", "TRIM")


@dataclass
class ClampEvent:
    ticker: str
    original_pct: float
    clamped_pct: float   # always MAX_POSITION_PCT


def clamp_position_weights(trades: list[dict]) -> list[ClampEvent]:
    """Clamp target_weight_pct to <= MAX_POSITION_PCT for BUY/DOUBLE_DOWN/TRIM,
    in place. Returns one ClampEvent per trade actually clamped."""
    events: list[ClampEvent] = []
    for t in trades:
        if t.get("action") not in _CLAMPED_ACTIONS:
            continue
        wt = t.get("target_weight_pct")
        if wt is None:
            continue
        if wt > MAX_POSITION_PCT:
            events.append(ClampEvent((t.get("ticker") or "?").upper(), float(wt), MAX_POSITION_PCT))
            t["target_weight_pct"] = MAX_POSITION_PCT
    return events
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_risk_guardrails.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/risk_guardrails.py tests/test_risk_guardrails.py
git commit -m "feat: risk_guardrails module + clamp_position_weights"
```

---

### Task 2: `compute_sector_exposure` + `sector_warnings`

**Files:**
- Modify: `app/risk_guardrails.py` (append)
- Test: `tests/test_risk_guardrails.py` (append)

**Interfaces:**
- Produces: `compute_sector_exposure(positions, trades, portfolio_value, sector_of) -> dict[str, float]`; `sector_warnings(exposure) -> list[str]`. `positions` items: `{"symbol", "qty", "current_price", ...}`. `sector_of`: `ticker -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_risk_guardrails.py
from app.risk_guardrails import compute_sector_exposure, sector_warnings


def test_sector_exposure_post_trade_weights():
    positions = [
        {"symbol": "NVDA", "qty": 1, "current_price": 100.0},   # current 10%
        {"symbol": "AMD",  "qty": 1, "current_price": 200.0},   # current 20%
        {"symbol": "KO",   "qty": 1, "current_price": 100.0},   # current 10%
    ]
    trades = [
        {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25},
        {"action": "SELL", "ticker": "AMD"},                    # -> 0
        {"action": "BUY", "ticker": "AVGO", "target_weight_pct": 30},  # new, tech
        # KO has no trade -> stays at current 10%
    ]
    sectors = {"NVDA": "Technology", "AMD": "Technology", "AVGO": "Technology", "KO": "Consumer Defensive"}
    exposure = compute_sector_exposure(positions, trades, 1000.0, sectors.get)
    assert round(exposure["Technology"], 1) == 55.0        # 25 (NVDA) + 0 (AMD sold) + 30 (AVGO)
    assert round(exposure["Consumer Defensive"], 1) == 10.0


def test_unknown_sector_bucketed():
    positions = [{"symbol": "XYZ", "qty": 1, "current_price": 600.0}]
    exposure = compute_sector_exposure(positions, [], 1000.0, lambda t: None)
    assert exposure == {"Unknown": 60.0}


def test_sector_warnings_only_over_cap_excluding_unknown():
    assert sector_warnings({"Technology": 68.0, "Energy": 40.0, "Unknown": 90.0}) == \
        ["Technology 68% (> 50% cap)"]
    assert sector_warnings({"Technology": 50.0}) == []   # not strictly over
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_risk_guardrails.py::test_sector_exposure_post_trade_weights -v`
Expected: FAIL with `ImportError: cannot import name 'compute_sector_exposure'`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/risk_guardrails.py`:

```python
def compute_sector_exposure(positions, trades, portfolio_value, sector_of) -> dict:
    """Post-trade percent-of-portfolio by sector. None-sector tickers bucket into 'Unknown'."""
    if not portfolio_value or portfolio_value <= 0:
        return {}
    trade_by_ticker = {t["ticker"].upper(): t for t in trades if t.get("ticker")}
    weights: dict[str, float] = {}
    held: set[str] = set()
    for p in positions:
        sym = p["symbol"].upper()
        held.add(sym)
        t = trade_by_ticker.get(sym)
        action = t.get("action") if t else None
        if action == "SELL":
            weights[sym] = 0.0
        elif action in ("BUY", "DOUBLE_DOWN", "TRIM"):
            weights[sym] = float(t.get("target_weight_pct") or 0.0)
        else:  # HOLD or no matching trade
            weights[sym] = (p.get("qty", 0) * p.get("current_price", 0)) / portfolio_value * 100
    for tk, t in trade_by_ticker.items():
        if tk not in held and t.get("action") in ("BUY", "DOUBLE_DOWN"):
            weights[tk] = float(t.get("target_weight_pct") or 0.0)
    exposure: dict[str, float] = {}
    for tk, w in weights.items():
        if w <= 0:
            continue
        sector = sector_of(tk) or "Unknown"
        exposure[sector] = exposure.get(sector, 0.0) + w
    return exposure


def sector_warnings(exposure: dict) -> list:
    """Sectors (excluding 'Unknown') strictly above MAX_SECTOR_PCT, highest first."""
    over = [(s, p) for s, p in exposure.items() if s != "Unknown" and p > MAX_SECTOR_PCT]
    over.sort(key=lambda x: x[1], reverse=True)
    return [f"{s} {p:.0f}% (> {MAX_SECTOR_PCT:.0f}% cap)" for s, p in over]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_risk_guardrails.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add app/risk_guardrails.py tests/test_risk_guardrails.py
git commit -m "feat: compute_sector_exposure + sector_warnings"
```

---

### Task 3: `resolve_sectors`, `_yf_sector_fetch`, `format_guardrail_embed`

**Files:**
- Modify: `app/risk_guardrails.py` (append)
- Test: `tests/test_risk_guardrails.py` (append)

**Interfaces:**
- Produces: `resolve_sectors(enriched, trades, fetch_sector) -> tuple[dict, list]`; `_yf_sector_fetch(ticker) -> str | None`; `format_guardrail_embed(clamps, warnings, unknown_tickers) -> dict | None`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_risk_guardrails.py
from app.risk_guardrails import resolve_sectors, format_guardrail_embed, ClampEvent


def test_resolve_sectors_uses_enriched_then_fetch():
    enriched = [{"ticker": "NVDA", "sector": "Technology"}]
    trades = [
        {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25},  # known from enriched
        {"action": "BUY", "ticker": "AVGO", "target_weight_pct": 20},          # needs fetch -> ok
        {"action": "BUY", "ticker": "ZZZ", "target_weight_pct": 10},           # fetch -> None
    ]
    calls = {"AVGO": "Technology", "ZZZ": None}
    sector_map, unknown = resolve_sectors(enriched, trades, lambda t: calls.get(t))
    assert sector_map["NVDA"] == "Technology"
    assert sector_map["AVGO"] == "Technology"
    assert unknown == ["ZZZ"]


def test_embed_none_when_nothing_fired():
    assert format_guardrail_embed([], [], []) is None


def test_embed_has_clamp_and_sector_fields():
    embed = format_guardrail_embed(
        [ClampEvent("NVDA", 32.0, 25.0)],
        ["Technology 68% (> 50% cap)"],
        ["ZZZ"],
    )
    assert embed["title"] == "⚠️ RISK GUARDRAIL"
    joined = " ".join(f["value"] for f in embed["fields"])
    assert "NVDA" in joined and "Technology 68%" in joined and "ZZZ" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_risk_guardrails.py::test_resolve_sectors_uses_enriched_then_fetch -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_sectors'`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/risk_guardrails.py`. `_yf_sector_fetch` and `format_guardrail_embed` use **lazy** imports:

```python
def resolve_sectors(enriched, trades, fetch_sector):
    """Build {ticker: sector|None} from enriched holdings, filling any BUY/DOUBLE_DOWN
    candidate not already known via fetch_sector. Returns (sector_map, unknown_tickers)."""
    sector_map: dict = {}
    for h in enriched:
        tk = (h.get("ticker") or "").upper()
        if tk:
            sector_map[tk] = h.get("sector")
    unknown: list = []
    for t in trades:
        if t.get("action") not in ("BUY", "DOUBLE_DOWN"):
            continue
        tk = (t.get("ticker") or "").upper()
        if not tk:
            continue
        if sector_map.get(tk) is None:
            sec = fetch_sector(tk)
            sector_map[tk] = sec
            if sec is None and tk not in unknown:
                unknown.append(tk)
    return sector_map, unknown


def _yf_sector_fetch(ticker: str):
    """Bounded yfinance sector lookup for a single ticker, or None on failure."""
    try:
        import yfinance as yf
        from app.pnl import _yf_fetch
        info = _yf_fetch(lambda: yf.Ticker(ticker).info)
        return info.get("sector") if info else None
    except Exception as exc:
        log.warning("risk_guardrails sector fetch failed for %s: %s", ticker, exc)
        return None


def format_guardrail_embed(clamps, warnings, unknown_tickers):
    """Combined ⚠️ RISK GUARDRAIL embed, or None when nothing fired."""
    if not clamps and not warnings:
        return None
    from app.claude_manager import _embed, _field, _CLR_ORANGE, _timestamp
    fields = []
    if clamps:
        lines = [f"{c.ticker}: {c.original_pct:.0f}% → clamped to {c.clamped_pct:.0f}%" for c in clamps]
        fields.append(_field("Position cap (25%)", "\n".join(lines), inline=False))
    if warnings:
        note = "\n".join(warnings)
        if unknown_tickers:
            note += f"\n(sector unresolved: {', '.join(unknown_tickers)})"
        fields.append(_field("Sector concentration (> 50%)", note, inline=False))
    return _embed("⚠️ RISK GUARDRAIL", _CLR_ORANGE, fields=fields, footer=_timestamp())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_risk_guardrails.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add app/risk_guardrails.py tests/test_risk_guardrails.py
git commit -m "feat: resolve_sectors + sector fetch + guardrail embed"
```

---

### Task 4: Wire into the monthly rebalance

**Files:**
- Modify: `app/claude_manager.py` — top import; `run_monthly_rebalance` immediately after `trades = [...]` (~line 1110), before the KI decisions card / execution.

**Interfaces:**
- Consumes: `clamp_position_weights`, `resolve_sectors`, `_yf_sector_fetch`, `compute_sector_exposure`, `sector_warnings`, `format_guardrail_embed` (Tasks 1–3). Uses in-scope `enriched`, `positions`, `portfolio_value`, `notify_claude_manager_embed`.

- [ ] **Step 1: Add the import**

Near the other `from app....` imports at the top of `app/claude_manager.py`, add:

```python
from app.risk_guardrails import (
    clamp_position_weights, resolve_sectors, _yf_sector_fetch,
    compute_sector_exposure, sector_warnings, format_guardrail_embed,
)
```

- [ ] **Step 2: Insert the guardrail block**

Immediately after the line `trades = [t for t in trade_block.get("trades", []) if t.get("ticker", "").upper() not in _EXCLUDED]` (~line 1110), and before the `# KI Server: research sections then decisions card` comment, insert:

```python
        # Risk guardrails: clamp any position to <=25% (safety-critical, always),
        # then best-effort sector-concentration alert (>50%, no auto-scale).
        _clamps = clamp_position_weights(trades)
        _warnings: list = []
        _unknown: list = []
        try:
            _sector_map, _unknown = resolve_sectors(enriched, trades, _yf_sector_fetch)
            _warnings = sector_warnings(
                compute_sector_exposure(positions, trades, portfolio_value, _sector_map.get)
            )
        except Exception as exc:
            log.warning("Rebalance sector guardrail check failed: %s", exc)
        _guardrail_embed = format_guardrail_embed(_clamps, _warnings, _unknown)
        if _guardrail_embed:
            await notify_claude_manager_embed(_guardrail_embed)
```

Because this runs before the execution loops read `target_weight_pct`, the clamped weights flow through the existing sizing math (and the expected-sell-proceeds calc) with no other change.

- [ ] **Step 3: Run targeted + full suite**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_claude_manager_history.py tests/test_claude_manager_section_ticker.py tests/test_risk_guardrails.py -v`
Expected: PASS.

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/ -q --ignore=tests/test_public_stats.py`
Expected: exactly the 8 pre-existing failures, no others (confirms clean import — the new `risk_guardrails` import plus its lazy `claude_manager` import must not cycle).

- [ ] **Step 4: Commit**

```bash
git add app/claude_manager.py
git commit -m "feat: enforce position/sector guardrails in monthly rebalance"
```

---

### Task 5: Wire into the weekly inspection

**Files:**
- Modify: `app/claude_inspection.py` — top import; `run_weekly_inspection` immediately after `portfolio_value = holdings_value + buying_power` (~line 223), before the expected-proceeds calc / Phase 1.

**Interfaces:**
- Consumes: the same six helpers. Uses in-scope `enriched`, `positions`, `portfolio_value`, `pending_trades`, `notify_claude_manager_embed`.

- [ ] **Step 1: Add the import**

Near the other imports at the top of `app/claude_inspection.py`, add:

```python
from app.risk_guardrails import (
    clamp_position_weights, resolve_sectors, _yf_sector_fetch,
    compute_sector_exposure, sector_warnings, format_guardrail_embed,
)
```

- [ ] **Step 2: Insert the guardrail block**

Immediately after `portfolio_value = holdings_value + buying_power` (~line 223), insert (note: the inspection's trade list is `pending_trades`):

```python
        # Risk guardrails: clamp DOUBLE_DOWN/TRIM to <=25% (always), then
        # best-effort sector-concentration alert (>50%, no auto-scale).
        _clamps = clamp_position_weights(pending_trades)
        _warnings: list = []
        _unknown: list = []
        try:
            _sector_map, _unknown = resolve_sectors(enriched, pending_trades, _yf_sector_fetch)
            _warnings = sector_warnings(
                compute_sector_exposure(positions, pending_trades, portfolio_value, _sector_map.get)
            )
        except Exception as exc:
            log.warning("Inspection sector guardrail check failed: %s", exc)
        _guardrail_embed = format_guardrail_embed(_clamps, _warnings, _unknown)
        if _guardrail_embed:
            await notify_claude_manager_embed(_guardrail_embed)
```

- [ ] **Step 3: Run targeted + full suite**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_claude_inspection_run.py tests/test_claude_inspection_parse.py tests/test_claude_inspection_log.py tests/test_claude_inspection_thesis_map.py tests/test_risk_guardrails.py -v`
Expected: PASS.

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/ -q --ignore=tests/test_public_stats.py`
Expected: exactly the 8 pre-existing failures, no others.

- [ ] **Step 4: Commit**

```bash
git add app/claude_inspection.py
git commit -m "feat: enforce position/sector guardrails in weekly inspection"
```

---

## Self-Review

**Spec coverage:**
- Constants 25/50 + clamped actions → Task 1. ✓
- Hard clamp (BUY/DOUBLE_DOWN/TRIM, in place, clamp-not-reject) → Task 1 `clamp_position_weights`. ✓
- Post-trade sector exposure + >50% warnings + Unknown bucket → Task 2. ✓
- New-candidate sector resolution via bounded fetch + unknown list → Task 3 `resolve_sectors` / `_yf_sector_fetch`. ✓
- Combined alert embed, None when nothing fired → Task 3 `format_guardrail_embed`. ✓
- Wiring both run functions; clamp first/unconditional, sector best-effort (try/except) → Tasks 4–5. ✓
- No import cycle (lazy claude_manager/pnl) → Tasks 1/3 lazy imports; Tasks 4/5 verify clean import via suite. ✓
- Sizing math unchanged (clamp mutates dict before loops) → Task 4/5 Step 2 placement before execution. ✓

**Placeholder scan:** No TBD/TODO/vague steps; every code step is complete. ✓

**Type consistency:** `clamp_position_weights(list[dict]) -> list[ClampEvent]`, `ClampEvent(ticker, original_pct, clamped_pct)`, `compute_sector_exposure(positions, trades, portfolio_value, sector_of) -> dict`, `sector_warnings(dict) -> list`, `resolve_sectors(enriched, trades, fetch_sector) -> (dict, list)`, `format_guardrail_embed(clamps, warnings, unknown) -> dict | None` used identically across Tasks 1–5. ✓
