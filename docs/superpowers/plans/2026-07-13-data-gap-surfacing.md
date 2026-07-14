# Per-Holding Data-Gap Surfacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when a holding was analyzed with a critical technical/fundamental field missing, and surface that both to Claude (inline in the prompt) and to the operator (in Discord), for both the monthly rebalance and the weekly inspection.

**Architecture:** Three pure helpers added to `app/claude_manager.py` (`compute_data_gaps`, `annotate_and_collect_gaps`, `format_data_gap_field`), consumed by both `run_monthly_rebalance` (`claude_manager.py`) and `run_weekly_inspection` (`claude_inspection.py`). Detection runs as a post-pass over the already-built `enriched` holdings list, so the fetch functions are untouched.

**Tech Stack:** Python 3.10+ (Render runtime), pytest. No new dependencies.

## Global Constraints

- Critical fields (the only ones flagged): `rsi`, `sma200_pct`, `perf_qtd`, `forward_pe`, `revenue_growth_yoy`. Copy this tuple verbatim.
- A field is "missing" when `holding.get(field) is None` (covers both `None` values and absent keys).
- Never blocking, never retrying, never modifying the fetch functions. Detection is additive and pure.
- Discord surfacing appears **only when at least one gap exists**.
- The existing all-empty "TECHNICAL DATA UNAVAILABLE" embed (`claude_manager.py:897`) stays unchanged.
- Run tests with the repo's pytest interpreter: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest`. (The default `python` lacks pytest.)

---

### Task 1: `compute_data_gaps` helper + critical field set

**Files:**
- Modify: `app/claude_manager.py` (add after `_TECHNICAL_KEYS`, line 377)
- Test: `tests/test_data_gap_surfacing.py` (create)

**Interfaces:**
- Produces: `_CRITICAL_DATA_FIELDS: tuple[str, ...]` and `compute_data_gaps(holding: dict) -> list[str]` (sorted list of missing critical field names; `[]` when complete).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_gap_surfacing.py
import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from app.claude_manager import compute_data_gaps, _CRITICAL_DATA_FIELDS


def _full_holding():
    return {
        "ticker": "MSFT",
        "rsi": 55.0, "sma200_pct": 0.1, "perf_qtd": 0.05,
        "forward_pe": 30.0, "revenue_growth_yoy": 0.15,
        "short_pct_float": None,  # minor field, must NOT be flagged
    }


def test_full_holding_has_no_gaps():
    assert compute_data_gaps(_full_holding()) == []


def test_missing_technical_is_flagged():
    h = _full_holding(); h["rsi"] = None
    assert compute_data_gaps(h) == ["rsi"]


def test_missing_fundamental_is_flagged():
    h = _full_holding(); del h["forward_pe"]
    assert compute_data_gaps(h) == ["forward_pe"]


def test_minor_field_none_not_flagged():
    assert compute_data_gaps(_full_holding()) == []  # short_pct_float None ignored


def test_total_failure_flags_all_sorted():
    assert compute_data_gaps({"ticker": "X"}) == sorted(_CRITICAL_DATA_FIELDS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_data_gap_surfacing.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_data_gaps'`.

- [ ] **Step 3: Write minimal implementation**

Add after line 377 (the `_TECHNICAL_KEYS` definition) in `app/claude_manager.py`:

```python
_CRITICAL_DATA_FIELDS: tuple[str, ...] = (
    "rsi", "sma200_pct", "perf_qtd",      # technicals
    "forward_pe", "revenue_growth_yoy",   # core fundamentals
)


def compute_data_gaps(holding: dict) -> list[str]:
    """Return the sorted critical fields missing (None or absent) for a holding.

    Pure — no I/O. Empty list means the holding has full critical coverage.
    Minor/optional fields (e.g. short_pct_float) are intentionally not tracked.
    """
    return sorted(f for f in _CRITICAL_DATA_FIELDS if holding.get(f) is None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_data_gap_surfacing.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add app/claude_manager.py tests/test_data_gap_surfacing.py
git commit -m "feat: add compute_data_gaps critical-field detector"
```

---

### Task 2: `annotate_and_collect_gaps` post-pass helper

**Files:**
- Modify: `app/claude_manager.py` (add immediately below `compute_data_gaps`)
- Test: `tests/test_data_gap_surfacing.py` (append)

**Interfaces:**
- Consumes: `compute_data_gaps` (Task 1).
- Produces: `annotate_and_collect_gaps(enriched: list[dict]) -> dict[str, list[str]]` — mutates each holding in place, setting `holding["_data_gaps"] = [...]` when it has gaps, and returns `{ticker: gaps}` for every holding that had at least one gap.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_data_gap_surfacing.py
from app.claude_manager import annotate_and_collect_gaps


def test_annotate_sets_field_and_returns_map():
    enriched = [
        {"ticker": "MSFT", "rsi": 55.0, "sma200_pct": 0.1, "perf_qtd": 0.05,
         "forward_pe": 30.0, "revenue_growth_yoy": 0.15},
        {"ticker": "NVDA", "rsi": None, "sma200_pct": 0.2, "perf_qtd": 0.1,
         "forward_pe": 40.0, "revenue_growth_yoy": 0.5},
    ]
    result = annotate_and_collect_gaps(enriched)
    assert result == {"NVDA": ["rsi"]}
    assert "_data_gaps" not in enriched[0]           # complete holding untouched
    assert enriched[1]["_data_gaps"] == ["rsi"]      # gap holding annotated


def test_annotate_empty_when_all_complete():
    enriched = [{"ticker": "MSFT", "rsi": 55.0, "sma200_pct": 0.1, "perf_qtd": 0.05,
                 "forward_pe": 30.0, "revenue_growth_yoy": 0.15}]
    assert annotate_and_collect_gaps(enriched) == {}
    assert "_data_gaps" not in enriched[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_data_gap_surfacing.py::test_annotate_sets_field_and_returns_map -v`
Expected: FAIL with `ImportError: cannot import name 'annotate_and_collect_gaps'`.

- [ ] **Step 3: Write minimal implementation**

Add immediately below `compute_data_gaps` in `app/claude_manager.py`:

```python
def annotate_and_collect_gaps(enriched: list[dict]) -> dict[str, list[str]]:
    """Tag each holding that has critical data gaps and return {ticker: gaps}.

    Sets holding["_data_gaps"] in place (so it serializes into the prompt and
    is captured in the run log). Only holdings with >=1 gap appear in the map.
    """
    gaps_by_ticker: dict[str, list[str]] = {}
    for holding in enriched:
        gaps = compute_data_gaps(holding)
        if gaps:
            holding["_data_gaps"] = gaps
            gaps_by_ticker[holding.get("ticker", "?")] = gaps
    return gaps_by_ticker
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_data_gap_surfacing.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add app/claude_manager.py tests/test_data_gap_surfacing.py
git commit -m "feat: add annotate_and_collect_gaps post-pass"
```

---

### Task 3: `format_data_gap_field` Discord formatter

**Files:**
- Modify: `app/claude_manager.py` (add immediately below `annotate_and_collect_gaps`)
- Test: `tests/test_data_gap_surfacing.py` (append)

**Interfaces:**
- Consumes: `_field` (existing, `claude_manager.py:76`).
- Produces: `format_data_gap_field(gaps_by_ticker: dict[str, list[str]]) -> dict | None` — an embed field dict, or `None` when there are no gaps.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_data_gap_surfacing.py
from app.claude_manager import format_data_gap_field


def test_format_returns_none_when_empty():
    assert format_data_gap_field({}) is None


def test_format_builds_sorted_field():
    field = format_data_gap_field({"NVDA": ["rsi"], "META": ["forward_pe", "revenue_growth_yoy"]})
    assert field["name"] == "⚠️ Data gaps"
    # tickers sorted deterministically; META before NVDA
    assert field["value"] == "META (forward_pe, revenue_growth_yoy); NVDA (rsi)"
    assert field["inline"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_data_gap_surfacing.py::test_format_builds_sorted_field -v`
Expected: FAIL with `ImportError: cannot import name 'format_data_gap_field'`.

- [ ] **Step 3: Write minimal implementation**

Add immediately below `annotate_and_collect_gaps` in `app/claude_manager.py`:

```python
def format_data_gap_field(gaps_by_ticker: dict[str, list[str]]) -> dict | None:
    """Return an embed field summarizing per-ticker data gaps, or None if empty."""
    if not gaps_by_ticker:
        return None
    parts = [f"{tk} ({', '.join(gaps)})" for tk, gaps in sorted(gaps_by_ticker.items())]
    return _field("⚠️ Data gaps", "; ".join(parts), inline=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_data_gap_surfacing.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add app/claude_manager.py tests/test_data_gap_surfacing.py
git commit -m "feat: add format_data_gap_field Discord formatter"
```

---

### Task 4: Wire into the monthly rebalance

**Files:**
- Modify: `app/claude_manager.py` — `_SYSTEM_PROMPT` (~line 137), `run_monthly_rebalance` enrichment tail (~line 938), analysis header embed (~line 1003).

**Interfaces:**
- Consumes: `annotate_and_collect_gaps`, `format_data_gap_field` (Tasks 2–3).

- [ ] **Step 1: Add the prompt instruction line**

In `_SYSTEM_PROMPT`, immediately after the macro-context bullet (line 137, `- Use macro context (VIX, 10Y yield, CPI)...`), add:

```
- If a holding's JSON includes a "_data_gaps" field, those listed metrics were unavailable this run — weight your analysis toward the available data and note the limitation in your reasoning.
```

- [ ] **Step 2: Collect gaps after the enrichment loop**

The enrichment loop ends where `enriched` is fully built. Immediately **before** `log_entry["positions_before"] = enriched` (line 939), insert:

```python
        data_gaps_by_ticker = annotate_and_collect_gaps(enriched)
```

(Placing it before line 939 means the gaps are also captured in `log_entry["positions_before"]` and serialized into the prompt at line 950 for free.)

- [ ] **Step 3: Attach the gap field to the analysis header embed**

Replace the `analysis_header = _embed(...)` call (lines 1003–1014) so it passes a `fields` argument. Add this line just before the `_embed(` call:

```python
        _gap_field = format_data_gap_field(data_gaps_by_ticker)
```

Then add `fields=[_gap_field] if _gap_field else None,` to the `_embed(` keyword arguments (e.g. immediately after the `description=(...)` block, before `footer=_timestamp(),`).

- [ ] **Step 4: Run the rebalance + full suite to verify no regression**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_claude_manager_history.py tests/test_claude_manager_section_ticker.py tests/test_data_gap_surfacing.py -v`
Expected: PASS.

Then confirm the whole suite is no worse than the known baseline (8 pre-existing failures in `test_pnl`/`test_trade_notifier`, and the `test_public_stats` collection error — all unrelated):

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/ -q --ignore=tests/test_public_stats.py`
Expected: same 8 pre-existing failures, no new ones.

- [ ] **Step 5: Commit**

```bash
git add app/claude_manager.py
git commit -m "feat: surface per-holding data gaps in monthly rebalance"
```

---

### Task 5: Wire into the weekly inspection

**Files:**
- Modify: `app/claude_inspection.py` — imports (line 19–22), `_INSPECTION_SYSTEM_PROMPT` (~line 557), `run_weekly_inspection` enrichment tail (~line 122).

**Interfaces:**
- Consumes: `annotate_and_collect_gaps`, `format_data_gap_field` (Tasks 2–3), plus already-imported `_embed`, `_CLR_ORANGE`, `_timestamp`, `notify_claude_manager_embed`.

- [ ] **Step 1: Import the helpers**

Change the import block at lines 19–22 to add the two helpers:

```python
from app.claude_manager import (
    _embed, _timestamp, _fetch_yf_data, _fetch_technical_data,
    _CLR_ORANGE, _CLR_GREEN, _CLR_GRAY, _LOG_PATH,
    annotate_and_collect_gaps, format_data_gap_field,
)
```

- [ ] **Step 2: Add the prompt instruction line**

In `_INSPECTION_SYSTEM_PROMPT`, immediately after the position-sizing paragraph ending `...SPY is permanently excluded — never mention it.` (line 557), add a new paragraph:

```
If a holding's JSON includes a "_data_gaps" field, those listed metrics were unavailable this run — weight your analysis toward the available data and note the limitation.
```

- [ ] **Step 3: Collect gaps and post a standalone embed when present**

Immediately after the enrichment loop that builds `enriched` (right after line 121, before `log_entry["holdings_reviewed"] = ...` on line 122), insert:

```python
        data_gaps_by_ticker = annotate_and_collect_gaps(enriched)
        _gap_field = format_data_gap_field(data_gaps_by_ticker)
        if _gap_field:
            await notify_claude_manager_embed(_embed(
                "⚠️ DATA GAPS THIS RUN",
                _CLR_ORANGE,
                description="Some holdings were reviewed with incomplete data (see fields). "
                            "Claude was told to weight toward available metrics.",
                fields=[_gap_field],
                footer=_timestamp(),
            ))
```

(Inspection has no single consolidated report embed like the rebalance's analysis header, so a standalone embed — mirroring the rebalance's own "TECHNICAL DATA UNAVAILABLE" pattern — is the equivalent surface. It fires regardless of the trade / no-changes branch.)

- [ ] **Step 4: Run inspection tests + full suite to verify no regression**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_claude_inspection_run.py tests/test_claude_inspection_parse.py tests/test_claude_inspection_log.py tests/test_claude_inspection_thesis_map.py tests/test_data_gap_surfacing.py -v`
Expected: PASS.

Then the whole suite against baseline:

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/ -q --ignore=tests/test_public_stats.py`
Expected: same 8 pre-existing failures, no new ones.

- [ ] **Step 5: Commit**

```bash
git add app/claude_inspection.py
git commit -m "feat: surface per-holding data gaps in weekly inspection"
```

---

## Self-Review

**Spec coverage:**
- Critical field set → Task 1 (`_CRITICAL_DATA_FIELDS`). ✓
- Shared detector in `claude_manager.py` → Tasks 1–3. ✓
- Attach gaps during enrichment (both systems) → Tasks 4 (Step 2), 5 (Step 3) via `annotate_and_collect_gaps`. ✓
- Inline `_data_gaps` reaches the prompt → rebalance serializes `enriched` at `:950`; inspection at `:128`. ✓
- One prompt instruction line each → Task 4 Step 1, Task 5 Step 2. ✓
- Discord only when gaps exist → `format_data_gap_field` returns `None` when empty; rebalance passes `fields=None`, inspection skips the embed. ✓
- Existing all-empty warning untouched → not modified in any task. ✓
- Testing per spec → Task 1–3 cover all six enumerated cases; wiring verified via existing suites. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows complete code. ✓

**Type consistency:** `compute_data_gaps(holding: dict) -> list[str]`, `annotate_and_collect_gaps(enriched: list[dict]) -> dict[str, list[str]]`, `format_data_gap_field(dict) -> dict | None`, and `_data_gaps` field name are used identically across Tasks 1–5. ✓
