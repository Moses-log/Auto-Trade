# Per-Holding Data-Gap Surfacing — Design Spec

## Goal

Both Kimi Manager (monthly rebalance) and Kimi Inspection (weekly check-in) enrich each
holding with technical + fundamental data before handing it to Claude. Those fetches fail
silently: `_fetch_technical_data` returns `{}` on any error (`app/claude_manager.py:428`) and
`_fetch_yf_data` returns `{"ticker": ...}` on total failure (`app/claude_manager.py:370`), with
individual fields simply coming back `None`. The **only** alarm is when *every* holding's
technicals are empty (`app/claude_manager.py:897`). So if a single holding loses its RSI,
200-day MA, or forward P/E while the others succeed, it flows into Claude's context — and a
SELL/HOLD/DOUBLE_DOWN decision gets made on it — with no signal to Claude or to the operator
that the analysis was partial. This is the same silent-failure class that let the Finviz outage
run undetected for a full rebalance.

This spec adds a per-holding data-gap detector that (a) tells Claude, inline, which critical
metrics were unavailable for a given holding, and (b) surfaces those gaps to the operator in the
Discord report — for both systems, from one shared implementation.

## Non-goals

- **No blocking or retry.** A gap is informational only; the run proceeds exactly as today. We
  never skip a holding or abort a run because data is missing.
- **No changes to the fetch functions themselves** (`_fetch_technical_data`, `_fetch_yf_data`).
  Detection is purely additive, downstream of the existing enrichment merge.
- **No severity tiers.** A field is either critical (flagged) or not tracked. (The tiered
  variant was considered and rejected for noise/complexity.)
- **No replacement of the existing all-empty warning.** The systemic "TECHNICAL DATA
  UNAVAILABLE" embed (`claude_manager.py:897`) stays as the louder total-failure alarm; this
  feature is the finer-grained per-holding layer beneath it.
- **No new data sources.** Improving `short_pct_float` reliability is out of scope (separate item).

## Critical field set

Only these fields trigger a gap flag when `None`/absent in the merged holding dict:

| Category | Fields |
|---|---|
| Technicals | `rsi`, `sma200_pct`, `perf_qtd` |
| Fundamentals | `forward_pe`, `revenue_growth_yoy` |

Everything else — including the routinely-absent `short_pct_float` and secondary fundamentals
(`peg_ratio`, `ev_ebitda`, etc.) — is deliberately **not** flagged, to keep signal high and
Discord quiet on healthy runs.

## Current Behavior (for reference)

- **Rebalance** (`claude_manager.py`): enrichment loop merges `yf_data` + `fv_data` into a `data`
  dict per holding (~`:912–923`), computing `rs_vs_spy_qtd` and weight, then serializes all
  holdings via `json.dumps` into the prompt.
- **Inspection** (`claude_inspection.py`): parallel enrichment loop builds the same shape of
  merged dict (~`:111–121`), serialized via `_json.dumps(enriched)` into the prompt (`:128`).
- Inspection already imports shared helpers from `claude_manager` (`_fetch_technical_data`,
  `_fetch_yf_data`, `_parse_trade_block`, `_DIVIDER`, `_section_ticker`), so shared logic added
  to `claude_manager` is consumed there with a one-line import.

## New Behavior

### 1. Shared detector in `claude_manager.py`

```python
_CRITICAL_DATA_FIELDS: tuple[str, ...] = (
    "rsi", "sma200_pct", "perf_qtd",      # technicals
    "forward_pe", "revenue_growth_yoy",   # fundamentals
)


def compute_data_gaps(holding: dict) -> list[str]:
    """Return the sorted critical fields that are missing (None or absent) for a
    holding. Pure — no I/O. Empty list means the holding is fully covered."""
    return sorted(f for f in _CRITICAL_DATA_FIELDS if holding.get(f) is None)
```

Both are module-level in `claude_manager.py`; `compute_data_gaps` (and, if useful,
`_CRITICAL_DATA_FIELDS`) is imported by `claude_inspection.py`.

### 2. Attach gaps during enrichment (both systems)

Immediately after each system builds its merged per-holding `data` dict, before it is appended
to `enriched`:

```python
gaps = compute_data_gaps(data)
if gaps:
    data["_data_gaps"] = gaps
    data_gaps_by_ticker[data["ticker"]] = gaps
```

`data_gaps_by_ticker: dict[str, list[str]]` is initialized empty at the top of each run. Because
`_data_gaps` becomes a field on the holding dict, it is serialized into the prompt automatically
next to that ticker's data — no separate prompt section needed.

### 3. One instruction line in each system prompt

Add to both `_INSPECTION_SYSTEM_PROMPT` (`claude_inspection.py`) and the rebalance system prompt
(`claude_manager.py`):

> If a holding includes a `_data_gaps` field, those metrics were unavailable this run — weight
> your analysis toward the available data and note the limitation in your reasoning.

### 4. Discord surfacing — only when gaps exist

After enrichment, if `data_gaps_by_ticker` is non-empty, add one field to the existing report
embed (rebalance report and inspection report respectively). Silent when everything is complete.

A shared formatter in `claude_manager.py`:

```python
def format_data_gap_field(gaps_by_ticker: dict[str, list[str]]) -> dict | None:
    """Return an embed field summarizing per-ticker gaps, or None if there are none."""
    if not gaps_by_ticker:
        return None
    parts = [f"{tk} ({', '.join(gaps)})" for tk, gaps in sorted(gaps_by_ticker.items())]
    return _field("⚠️ Data gaps", "; ".join(parts), inline=False)
```

Rendered example: `⚠️ Data gaps — NVDA (rsi); META (forward_pe, revenue_growth_yoy)`.

## Data flow (identical in both systems)

```
fetch (unchanged) → merge into `data` → compute_data_gaps(data)
  → if gaps: data["_data_gaps"]=gaps ; record in data_gaps_by_ticker
  → holdings serialized to prompt (gaps ride inline)
  → Claude decides, guided by the new prompt line
  → report embed: append format_data_gap_field(data_gaps_by_ticker) if not None
```

## Error handling

`compute_data_gaps` is pure and cannot fail on well-formed input; a malformed/partial holding
dict (e.g. the `{"ticker": X}` total-failure shape) simply reports all five fields as missing,
which is the correct outcome. No try/except needed. The feature never raises into the run path.

## Testing

New unit tests (`tests/test_data_gap_surfacing.py`):

1. `compute_data_gaps` — fully-populated holding → `[]`.
2. Missing a technical (`rsi=None`) → `["rsi"]`.
3. Missing a fundamental (`forward_pe` absent) → `["forward_pe"]`.
4. `short_pct_float=None` with all critical fields present → `[]` (proves minor fields aren't flagged).
5. Total-failure dict `{"ticker": "X"}` → all five critical fields, sorted.
6. `format_data_gap_field({})` → `None`; non-empty map → a field with the expected text and
   deterministic (sorted) ticker order.

## Files touched

- `app/claude_manager.py` — add `_CRITICAL_DATA_FIELDS`, `compute_data_gaps`,
  `format_data_gap_field`; wire gap detection into the rebalance enrichment loop; add prompt
  line; append embed field to the rebalance report.
- `app/claude_inspection.py` — import the helpers; wire gap detection into the inspection
  enrichment loop; add prompt line; append embed field to the inspection report.
- `tests/test_data_gap_surfacing.py` — new.
