# Decision → Outcome Feedback Loop — Design Spec

## Goal

Kimi Manager (monthly rebalance) and Kimi Inspection (weekly check-in) are effectively
stateless about their own track record. `_load_recent_history` pulls the last few rebalances'
*theses* into context, but nothing tells Claude whether its past *decisions* worked — it sells a
name and never learns whether that name then dropped 20% (good sell) or rallied 30% (bad sell).
The raw material exists but is never joined: executed trades are in `claude_rebalance_log.json`
and `claude_inspection_log.json`, and per-ticker prices are available via yfinance.

This spec adds a feedback loop: score every past executed trade by how the stock performed
**relative to SPY since the decision**, aggregate into a compact scorecard, inject that scorecard
into the next rebalance and inspection prompts (so Claude calibrates from its own history), and
post a monthly "Decision Review" embed to Discord for the operator.

## Non-goals

- **No new persisted state.** Outcomes are derived live from the existing decision logs +
  yfinance on each run. No new data file, no backup wiring, no migration.
- **Only executed trades are scored** — `BUY`, `SELL`, `TRIM`, `DOUBLE_DOWN`. `HOLD` decisions
  and proposed-but-skipped trades are out of scope for this version.
- **No change to how decisions are made or executed** — this is a read-only analysis layer that
  adds context to the prompt and a report to Discord. It never places or blocks a trade.
- **No weekly Discord review.** The scorecard is injected into both prompts every run, but the
  Discord "Decision Review" embed fires monthly (with the rebalance) only, to avoid noise.

## Attribution method

For each executed decision with a decision date `D` and ticker `T`:

```
stock_return = adj_close(T, latest) / adj_close(T, D) - 1
spy_return   = adj_close(SPY, latest) / adj_close(SPY, D) - 1
rel          = stock_return - spy_return
```

Both legs use **yfinance auto-adjusted closes** (handles splits/dividends), decision-date → latest,
for both the stock and SPY. Using yfinance for both legs keeps rebalance and inspection uniform
and does not depend on SPY being recorded in the inspection log. The decision date `D` is the log
entry's `timestamp` (all executed trades in a run share it).

**Verdict** (with a neutral dead-band to avoid crediting noise; `NEUTRAL_BAND = 0.015` = 1.5%):

| Action | Verdict rule |
|---|---|
| `SELL`, `TRIM` | `rel < -NEUTRAL_BAND` → **good** (stock underperformed SPY after exit — dodged); `rel > NEUTRAL_BAND` → **bad** (missed relative upside); else **neutral** |
| `BUY`, `DOUBLE_DOWN` | `rel > NEUTRAL_BAND` → **good** (beat SPY after adding); `rel < -NEUTRAL_BAND` → **bad**; else **neutral** |

## Scope of decisions

- Source: both `claude_rebalance_log.json` and `claude_inspection_log.json`, `trades_executed`.
- Window: executed decisions from the **last 6 months (183 days)**, then capped at the **20 most
  recent** by decision date, so the prompt scorecard stays compact. Older/excess decisions age out.
- A decision whose ticker returns no usable yfinance price data (delisted, fetch failure, or a
  decision date too recent to have a distinct close) is **skipped** and counted in a
  `skipped` tally, never guessed.

## Current Behavior (for reference)

- Rebalance builds its prompt (`claude_manager.py`, `run_monthly_rebalance`) and calls
  `_call_claude_sync`; it already computes SPY price and posts an analysis-header embed via
  `notify_claude_manager_embed`.
- Inspection builds its prompt (`claude_inspection.py`, `run_weekly_inspection`) via
  `_json.dumps(enriched)` + thesis map and calls `_call_claude_inspection_sync`.
- Both already use yfinance (`_fetch_technical_data`) and have a timeout helper pattern
  (`pnl._yf_fetch`) for bounded yfinance calls.
- Executed trades are recorded with `ticker` and `action` in each log's `trades_executed`; each
  log entry has a top-level `timestamp`.

## New Behavior

### New module: `app/decision_review.py`

Self-contained, one responsibility (scoring past decisions). Public interface:

```python
from dataclasses import dataclass

WINDOW_DAYS = 183
MAX_DECISIONS = 20
NEUTRAL_BAND = 0.015

@dataclass
class Decision:
    date: str      # "YYYY-MM-DD" (decision date, from log timestamp)
    ticker: str
    action: str    # BUY | SELL | TRIM | DOUBLE_DOWN

@dataclass
class DecisionOutcome:
    decision: Decision
    stock_return: float
    spy_return: float
    rel: float
    verdict: str   # "good" | "bad" | "neutral"

@dataclass
class Scorecard:
    outcomes: list[DecisionOutcome]
    skipped: int                       # decisions with no usable price data
    by_action: dict[str, tuple[int, int, int]]  # action -> (good, bad, neutral)


def load_executed_decisions(now: date | None = None) -> list[Decision]:
    """Read both decision logs, return executed BUY/SELL/TRIM/DOUBLE_DOWN within
    WINDOW_DAYS, newest first, capped at MAX_DECISIONS. Pure I/O over the logs."""

def score_decision(decision: Decision, price_fn) -> DecisionOutcome | None:
    """Compute stock/SPY returns and verdict. `price_fn(ticker, start_date) ->
    (start_close, latest_close) | None` is injected so it is unit-testable
    without network. Returns None when price data is unusable."""

def build_scorecard(decisions: list[Decision], price_fn) -> Scorecard:
    """Score all decisions, aggregate good/bad/neutral counts by action."""

def format_scorecard_prompt(sc: Scorecard) -> str:
    """Compact one-paragraph scorecard for injection into a Claude prompt.
    Returns "" when there are no scored decisions (nothing to inject)."""

def format_scorecard_embed(sc: Scorecard) -> dict:
    """Discord embed dict summarizing aggregates + the most recent decisions.
    Reuses claude_manager._embed / _field."""
```

`price_fn` is the yfinance adapter, kept separate so the scoring logic is pure and testable. The
production adapter fetches `yf.Ticker(t).history(start=D, auto_adjust=True)`, wrapped in the
existing bounded-fetch pattern, and returns the first and last available closes (or `None`).

### Prompt injection (both systems)

Before the prompt string is assembled, build the scorecard once and prepend its text when
non-empty:

- Rebalance (`run_monthly_rebalance`): `scorecard_text = format_scorecard_prompt(build_scorecard(load_executed_decisions(), price_fn))`; insert a `Decision Track Record (vs SPY):\n{scorecard_text}` block into the prompt ahead of the holdings JSON. Add one system-prompt line: *"A 'Decision Track Record' section may be provided — use it to calibrate: repeat what worked, reconsider what didn't."*
- Inspection (`run_weekly_inspection`): same scorecard text block inserted into its prompt, and the same one-line instruction added to `_INSPECTION_SYSTEM_PROMPT`.

Empty scorecard (no scored decisions yet — e.g. first months) → inject nothing, add no noise.

### Discord Decision Review (monthly, rebalance only)

In `run_monthly_rebalance`, after the scorecard is built and if it has ≥1 scored outcome, post
`format_scorecard_embed(sc)` via `notify_claude_manager_embed` (Private Server `#manager`). The
inspection does **not** post this embed.

## Data flow

```
logs (rebalance + inspection) --load_executed_decisions--> [Decision] (<=20, <=6mo)
   --build_scorecard(price_fn=yfinance)--> Scorecard(outcomes, skipped, by_action)
   --format_scorecard_prompt--> injected into rebalance & inspection prompts
   --format_scorecard_embed---> monthly Discord "Decision Review" (rebalance only)
```

## Error handling

- Scoring is isolated behind `price_fn`; a ticker with no data returns `None` → skipped, tallied,
  never guessed. A network/timeout failure in the adapter returns `None` the same way.
- Building the scorecard must never raise into the rebalance/inspection run: wrap the
  `load/build` call in a try/except that logs and yields an empty scorecard, so a feedback-layer
  failure degrades to "no scorecard this run" rather than breaking a rebalance.
- yfinance calls run through the existing bounded-fetch/executor pattern so they cannot hang the
  run indefinitely.

## Testing

`tests/test_decision_review.py`:

1. `load_executed_decisions` — fixture logs with mixed actions: returns only executed
   BUY/SELL/TRIM/DOUBLE_DOWN, excludes HOLD and skipped, respects the 6-month window and the
   20-cap (newest first).
2. `score_decision` with a stub `price_fn`:
   - SELL where stock fell 10% while SPY rose 2% → `rel < 0` → **good**.
   - SELL where stock rose 15% while SPY rose 3% → **bad**.
   - BUY where stock beat SPY → **good**; BUY that lagged SPY → **bad**.
   - `rel` inside ±1.5% band → **neutral** (both a SELL and a BUY case).
   - `price_fn` returns `None` → `score_decision` returns `None`.
3. `build_scorecard` — aggregates good/bad/neutral by action; counts skipped when `price_fn`
   returns `None` for some tickers.
4. `format_scorecard_prompt` — non-empty scorecard yields the expected compact text; empty
   scorecard yields `""`.
5. `format_scorecard_embed` — returns an embed dict with the aggregate line and recent decisions.

Wiring into the two run functions is verified by running the existing rebalance/inspection test
suites for regression (same baseline: 8 pre-existing failures in `test_pnl`/`test_trade_notifier`).

## Files touched

- Create: `app/decision_review.py`, `tests/test_decision_review.py`.
- Modify: `app/claude_manager.py` — build scorecard, inject into prompt, add system-prompt line,
  post monthly Discord review embed.
- Modify: `app/claude_inspection.py` — build scorecard, inject into prompt, add system-prompt line.
