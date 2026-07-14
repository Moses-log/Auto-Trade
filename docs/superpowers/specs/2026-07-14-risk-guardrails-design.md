# Code-Enforced Risk Guardrails — Design Spec

## Goal

Kimi's risk policy — *"maximum position size 25%, no single sector above 50%"* — currently lives
**only as English in the system prompt** (`claude_manager.py` `_SYSTEM_PROMPT`). Nothing in the
execution code enforces it: each trade is sized as `portfolio_value × target_weight_pct / 100`
(`claude_manager.py:1391-1392`) and executes, capped only by available cash. So a Claude
miscalculation or drift — a single `target_weight_pct` of 32, or several 20% positions that push
one sector past 60% — executes against real money unchecked. `sector` is fetched for current
holdings (`claude_manager.py:353`) but never aggregated or checked anywhere.

This spec adds a code-level guardrail layer between the parsed trade block and execution:
**hard-clamp** any single position to ≤25%, and **alert (no auto-scale)** when post-trade sector
exposure would exceed 50%. It applies to the monthly rebalance and the weekly inspection's
`DOUBLE_DOWN` path, from one shared module.

## Non-goals

- **No reject-mode.** A position over 25% is clamped down and still executed, never dropped. The
  intent is to keep the position at a safe size, not to skip a good idea over a sizing quibble.
- **No sector auto-scaling.** A sector over 50% produces a Discord alert only; the operator
  decides which name to trim. Auto-scaling a sector breach is a judgment call (which position?)
  that belongs to the rebalance, not a blind rule that could clip a high-conviction winner.
- **No new config / env vars.** The 25% / 50% limits are module constants matching the stated
  policy — a single source of truth, not tunable knobs.
- **No change to the sizing math.** The clamp only lowers `target_weight_pct` in the trade dict
  before the existing execution code reads it. SELL is untouched (it reduces exposure).

## Limits

`MAX_POSITION_PCT = 25.0`, `MAX_SECTOR_PCT = 50.0` — module constants in `app/risk_guardrails.py`,
matching `_SYSTEM_PROMPT`'s stated policy verbatim.

## Current Behavior (for reference)

- Rebalance parses the trade block into `trades` (`trade_block["trades"]`) and executes in order
  SELL (`:1183`) → TRIM (`:1265`) → BUY/DOUBLE_DOWN (`:1388`). BUY/DOUBLE_DOWN sizing:
  `target_wt = trade.get("target_weight_pct", 10)`; `target_dollars = portfolio_value * target_wt / 100`.
  TRIM sizing: `target_wt = trade.get("target_weight_pct", 5)`.
- Inspection executes SELL → TRIM → DOUBLE_DOWN (`claude_inspection.py` Phases 1–3); DOUBLE_DOWN
  reads `target_wt = trade["target_weight_pct"]` and sizes the same way.
- `sector` is present on each enriched holding (`info.get("sector")`) but only for **current
  holdings** — brand-new BUY candidates have no sector fetched.
- Both run functions already have `notify_claude_manager_embed`, `_embed`, `_field` available.

## New Behavior

### New module: `app/risk_guardrails.py`

Focused, mostly pure, unit-testable. Lazy `claude_manager` import only inside the embed formatter
(same cycle-avoidance pattern as `decision_review.py`).

```python
from dataclasses import dataclass

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
    in place. Returns one ClampEvent per trade actually clamped. Trades at or
    below the cap, and SELL/HOLD, are untouched."""


def compute_sector_exposure(
    positions: list[dict],           # current holdings: {"symbol", "qty", "current_price", "sector"?}
    trades: list[dict],              # parsed (already clamped) trade block trades
    portfolio_value: float,
    sector_of,                       # callable: ticker -> str | None
) -> dict[str, float]:
    """Return post-trade percent-of-portfolio by sector. Post-trade weight per
    ticker: SELL -> 0; BUY/DOUBLE_DOWN/TRIM -> its target_weight_pct; HOLD or an
    existing holding with no matching trade -> its current weight
    (current_value / portfolio_value * 100). Tickers whose sector is None are
    bucketed under 'Unknown'."""


def sector_warnings(exposure: dict[str, float]) -> list[str]:
    """Sectors (excluding 'Unknown') strictly above MAX_SECTOR_PCT, formatted
    'Technology 68% (> 50% cap)', highest first."""


def format_guardrail_embed(clamps: list[ClampEvent], warnings: list[str],
                           unknown_tickers: list[str]) -> dict | None:
    """Combined ⚠️ RISK GUARDRAIL embed, or None when nothing fired. Lazy-imports
    _embed / _field / _CLR_ORANGE / _timestamp from claude_manager."""
```

### Wiring — rebalance (`run_monthly_rebalance`)

After the trade block is parsed and `trades` is available, **before** the SELL/TRIM/BUY execution
loops:

1. `clamps = clamp_position_weights(trades)` — mutates `trades` so the existing loops size against
   the clamped weights automatically.
2. Build `sector_of`: a dict from enriched holdings (`symbol -> sector`), with a bounded
   `_yf_fetch` sector lookup for any proposed BUY/DOUBLE_DOWN ticker not already present. A ticker
   whose sector can't be resolved maps to `None` (→ `Unknown`) and is collected in
   `unknown_tickers`.
3. `exposure = compute_sector_exposure(positions, trades, portfolio_value, sector_of.get)`;
   `warnings = sector_warnings(exposure)`.
4. `embed = format_guardrail_embed(clamps, warnings, unknown_tickers)`; if not `None`,
   `await notify_claude_manager_embed(embed)`.

### Wiring — inspection (`run_weekly_inspection`)

The inspection only increases exposure via `DOUBLE_DOWN`. After its trade block is parsed, before
Phase 3:

1. `clamps = clamp_position_weights(pending_trades)` (covers its DOUBLE_DOWN/TRIM targets).
2. Same sector computation over current holdings + any DOUBLE_DOWN tickers, with bounded sector
   lookup for anything missing.
3. Post the same combined guardrail embed when non-empty.

## Data flow

```
parsed trades ──clamp_position_weights──> trades mutated (<=25% each) + [ClampEvent]
current positions + trades + sector_of ──compute_sector_exposure──> {sector: pct}
   ──sector_warnings──> [">50% sector" strings]
[ClampEvent] + [warnings] + [unknown] ──format_guardrail_embed──> ⚠️ RISK GUARDRAIL (if any)
execution loops read the already-clamped target_weight_pct — sizing code unchanged
```

## Error handling

- `clamp_position_weights`, `compute_sector_exposure`, `sector_warnings` are pure and cannot raise
  on well-formed input; a trade missing `target_weight_pct` is treated as not-clamped (nothing to
  clamp) and, for exposure, contributes 0 unless it is a current holding.
- The sector lookup for new candidates uses the bounded `pnl._yf_fetch`; a failed/timed-out lookup
  yields `None` → `Unknown`, never an exception into the run.
- The whole guardrail step is wrapped so it can never break a rebalance/inspection: on unexpected
  error, log and proceed to execution **with the position clamp already applied** (the clamp is the
  safety-critical half and runs first). Sector alerting is best-effort.

## Testing

`tests/test_risk_guardrails.py`:

1. `clamp_position_weights` — a BUY at 32% → clamped to 25 + one ClampEvent(32→25); DOUBLE_DOWN at
   40% → clamped; TRIM at 30% → clamped; BUY at 20% → untouched, no event; SELL/HOLD → untouched;
   trade missing `target_weight_pct` → untouched, no error.
2. `compute_sector_exposure` — current holdings + trades produce correct per-sector post-trade
   percentages (SELL→0, BUY at target, HOLD at current); a `None`-sector ticker lands in `Unknown`.
3. `sector_warnings` — a sector at 68% → one warning; all sectors ≤50% → empty; `Unknown` never
   warned even if >50%.
4. `format_guardrail_embed` — returns `None` when clamps and warnings are both empty; returns an
   embed dict with a clamp field and a sector field when both present.

Wiring into the two run functions is verified by the existing rebalance/inspection suites
(baseline: 8 pre-existing failures in `test_pnl`/`test_trade_notifier`).

## Files touched

- Create: `app/risk_guardrails.py`, `tests/test_risk_guardrails.py`.
- Modify: `app/claude_manager.py` — call the guardrail after parse / before execution; post embed.
- Modify: `app/claude_inspection.py` — same for the DOUBLE_DOWN path.
