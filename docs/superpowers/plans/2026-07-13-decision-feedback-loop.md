# Decision → Outcome Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score every past executed trade by the stock's return vs SPY since the decision, inject a compact scorecard into the rebalance + inspection prompts, and post a monthly Decision Review to Discord — all derived live from existing logs + yfinance, no new persisted state.

**Architecture:** A new self-contained module `app/decision_review.py` holds pure scoring/formatting logic plus a yfinance price adapter. `run_monthly_rebalance` (`app/claude_manager.py`) and `run_weekly_inspection` (`app/claude_inspection.py`) call one entry point, inject its text into their prompts, and (rebalance only) post the Discord embed.

**Tech Stack:** Python 3.10+ (Render runtime), yfinance, pytest. No new dependencies.

## Global Constraints

- Attribution: `rel = stock_return - spy_return` from decision date → latest, both legs yfinance auto-adjusted close.
- Verdict with neutral band `NEUTRAL_BAND = 0.015`: SELL/TRIM → good if `rel < -0.015`, bad if `rel > 0.015`, else neutral; BUY/DOUBLE_DOWN → good if `rel > 0.015`, bad if `rel < -0.015`, else neutral.
- Scope: only executed `BUY/SELL/TRIM/DOUBLE_DOWN` from both logs; `WINDOW_DAYS = 183`; cap `MAX_DECISIONS = 20`, newest first.
- No new persisted state. No import cycle: `decision_review.py` must NOT import `claude_manager` at module top (lazy import inside `format_scorecard_embed` only).
- A ticker with no usable price data is skipped and tallied, never guessed. Building the scorecard must never raise into a run.
- Empty scorecard → inject nothing into the prompt, post no embed.
- RUN TESTS WITH THIS EXACT INTERPRETER (default `python` lacks pytest):
  `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest`
- Full-suite baseline: exactly 8 pre-existing failures in `tests/test_pnl.py` and `tests/test_trade_notifier.py`; any other failure is a regression.

---

### Task 1: Module skeleton + `load_executed_decisions`

**Files:**
- Create: `app/decision_review.py`
- Test: `tests/test_decision_review.py` (create)

**Interfaces:**
- Produces: `Decision`, `DecisionOutcome`, `Scorecard` dataclasses; constants `WINDOW_DAYS=183`, `MAX_DECISIONS=20`, `NEUTRAL_BAND=0.015`; `load_executed_decisions(now: date | None = None) -> list[Decision]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_decision_review.py
import json
import os
from datetime import date

from app.decision_review import Decision, load_executed_decisions, MAX_DECISIONS


def _write_logs(tmp_path, rebalance_entries, inspection_entries):
    reb = tmp_path / "reb.json"; insp = tmp_path / "insp.json"
    reb.write_text(json.dumps(rebalance_entries)); insp.write_text(json.dumps(inspection_entries))
    os.environ["CLAUDE_REBALANCE_LOG_PATH"] = str(reb)
    os.environ["CLAUDE_INSPECTION_LOG_PATH"] = str(insp)


def test_extracts_only_executed_actions_within_window(tmp_path):
    _write_logs(tmp_path, [
        {"timestamp": "2026-07-01T09:35:00-05:00", "trades_executed": [
            {"action": "BUY", "ticker": "nvda"},
            {"action": "HOLD", "ticker": "MSFT"},        # excluded (not executed action)
        ]},
        {"timestamp": "2025-01-01T09:35:00-06:00", "trades_executed": [
            {"action": "SELL", "ticker": "OLD"},          # excluded (older than 183d)
        ]},
    ], [
        {"timestamp": "2026-07-08T15:00:00-05:00", "trades_executed": [
            {"action": "DOUBLE_DOWN", "ticker": "META"},
        ]},
    ])
    result = load_executed_decisions(now=date(2026, 7, 13))
    assert [(d.date, d.ticker, d.action) for d in result] == [
        ("2026-07-08", "META", "DOUBLE_DOWN"),   # newest first
        ("2026-07-01", "NVDA", "BUY"),
    ]


def test_caps_at_max_decisions(tmp_path):
    entries = [{"timestamp": f"2026-07-{d:02d}T09:35:00-05:00",
                "trades_executed": [{"action": "BUY", "ticker": f"T{d}"}]} for d in range(1, 13)]
    _write_logs(tmp_path, entries, [])
    result = load_executed_decisions(now=date(2026, 7, 13))
    assert len(result) == min(12, MAX_DECISIONS)
    assert result[0].date == "2026-07-12"  # newest first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_decision_review.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.decision_review'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/decision_review.py`:

```python
"""decision_review.py — Score past executed trades vs SPY and build a scorecard.

Derived live from the rebalance + inspection decision logs and yfinance prices.
No persisted state. Imports of app.claude_manager are lazy (inside functions)
to avoid an import cycle, since claude_manager imports this module.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import yfinance as yf

log = logging.getLogger(__name__)

WINDOW_DAYS = 183
MAX_DECISIONS = 20
NEUTRAL_BAND = 0.015

_EXECUTED_ACTIONS = {"BUY", "SELL", "TRIM", "DOUBLE_DOWN"}


@dataclass
class Decision:
    date: str      # "YYYY-MM-DD"
    ticker: str
    action: str


@dataclass
class DecisionOutcome:
    decision: Decision
    stock_return: float
    spy_return: float
    rel: float
    verdict: str   # "good" | "bad" | "neutral"


@dataclass
class Scorecard:
    outcomes: list = field(default_factory=list)
    skipped: int = 0
    by_action: dict = field(default_factory=dict)   # action -> (good, bad, neutral)


def _read_log(path: str) -> list:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_executed_decisions(now: date | None = None) -> list:
    """Executed BUY/SELL/TRIM/DOUBLE_DOWN from both logs within WINDOW_DAYS,
    newest first, capped at MAX_DECISIONS."""
    now = now or date.today()
    cutoff = now - timedelta(days=WINDOW_DAYS)
    paths = [
        os.getenv("CLAUDE_REBALANCE_LOG_PATH", "/data/claude_rebalance_log.json"),
        os.getenv("CLAUDE_INSPECTION_LOG_PATH", "/data/claude_inspection_log.json"),
    ]
    decisions: list = []
    for path in paths:
        for entry in _read_log(path):
            try:
                d = datetime.fromisoformat(entry.get("timestamp", "")).date()
            except Exception:
                continue
            if d < cutoff:
                continue
            for trade in entry.get("trades_executed", []):
                action = trade.get("action")
                ticker = trade.get("ticker")
                if action in _EXECUTED_ACTIONS and ticker:
                    decisions.append(Decision(date=d.isoformat(), ticker=ticker.upper(), action=action))
    decisions.sort(key=lambda x: x.date, reverse=True)
    return decisions[:MAX_DECISIONS]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_decision_review.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/decision_review.py tests/test_decision_review.py
git commit -m "feat: decision_review module + load_executed_decisions"
```

---

### Task 2: `score_decision` + `build_scorecard`

**Files:**
- Modify: `app/decision_review.py` (append after `load_executed_decisions`)
- Test: `tests/test_decision_review.py` (append)

**Interfaces:**
- Consumes: `Decision`, `DecisionOutcome`, `Scorecard`, `NEUTRAL_BAND` (Task 1).
- Produces: `score_decision(decision, price_fn) -> DecisionOutcome | None`; `build_scorecard(decisions, price_fn) -> Scorecard`. `price_fn(ticker: str, start_date: str) -> tuple[float, float] | None` returns `(start_close, latest_close)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_decision_review.py
from app.decision_review import score_decision, build_scorecard, DecisionOutcome


def _price_fn_factory(table):
    # table: {ticker: (start_close, latest_close) or None}
    def fn(ticker, start_date):
        return table.get(ticker)
    return fn


def test_sell_that_dodged_a_drop_is_good():
    d = Decision("2026-06-01", "XYZ", "SELL")
    # stock fell 10%, SPY rose 2% -> rel = -0.12 -> good sell
    fn = _price_fn_factory({"XYZ": (100.0, 90.0), "SPY": (100.0, 102.0)})
    o = score_decision(d, fn)
    assert o.verdict == "good" and round(o.rel, 2) == -0.12


def test_sell_that_missed_a_rally_is_bad():
    d = Decision("2026-06-01", "XYZ", "SELL")
    fn = _price_fn_factory({"XYZ": (100.0, 115.0), "SPY": (100.0, 103.0)})
    assert score_decision(d, fn).verdict == "bad"


def test_buy_that_beat_spy_is_good():
    d = Decision("2026-06-01", "XYZ", "BUY")
    fn = _price_fn_factory({"XYZ": (100.0, 120.0), "SPY": (100.0, 105.0)})
    assert score_decision(d, fn).verdict == "good"


def test_buy_that_lagged_spy_is_bad():
    d = Decision("2026-06-01", "XYZ", "DOUBLE_DOWN")
    fn = _price_fn_factory({"XYZ": (100.0, 101.0), "SPY": (100.0, 110.0)})
    assert score_decision(d, fn).verdict == "bad"


def test_within_neutral_band_is_neutral():
    fn = _price_fn_factory({"XYZ": (100.0, 101.0), "SPY": (100.0, 100.5)})  # rel = +0.005
    assert score_decision(Decision("2026-06-01", "XYZ", "SELL"), fn).verdict == "neutral"
    assert score_decision(Decision("2026-06-01", "XYZ", "BUY"), fn).verdict == "neutral"


def test_missing_price_returns_none():
    fn = _price_fn_factory({"XYZ": None, "SPY": (100.0, 102.0)})
    assert score_decision(Decision("2026-06-01", "XYZ", "SELL"), fn) is None


def test_build_scorecard_aggregates_and_counts_skipped():
    decisions = [
        Decision("2026-06-01", "AAA", "SELL"),   # good
        Decision("2026-06-02", "BBB", "BUY"),    # bad
        Decision("2026-06-03", "CCC", "SELL"),   # skipped (no data)
    ]
    fn = _price_fn_factory({
        "AAA": (100.0, 80.0), "BBB": (100.0, 90.0), "CCC": None,
        "SPY": (100.0, 100.0),
    })
    sc = build_scorecard(decisions, fn)
    assert sc.skipped == 1
    assert sc.by_action["SELL"] == (1, 0, 0)
    assert sc.by_action["BUY"] == (0, 1, 0)
    assert len(sc.outcomes) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_decision_review.py::test_sell_that_dodged_a_drop_is_good -v`
Expected: FAIL with `ImportError: cannot import name 'score_decision'`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/decision_review.py`:

```python
def score_decision(decision, price_fn):
    """Return a DecisionOutcome, or None when price data is unusable."""
    stock = price_fn(decision.ticker, decision.date)
    spy = price_fn("SPY", decision.date)
    if not stock or not spy:
        return None
    s0, s1 = stock
    p0, p1 = spy
    if not s0 or not p0:
        return None
    stock_return = s1 / s0 - 1
    spy_return = p1 / p0 - 1
    rel = stock_return - spy_return
    if decision.action in ("SELL", "TRIM"):
        verdict = "good" if rel < -NEUTRAL_BAND else "bad" if rel > NEUTRAL_BAND else "neutral"
    else:  # BUY, DOUBLE_DOWN
        verdict = "good" if rel > NEUTRAL_BAND else "bad" if rel < -NEUTRAL_BAND else "neutral"
    return DecisionOutcome(decision, round(stock_return, 4), round(spy_return, 4), round(rel, 4), verdict)


def build_scorecard(decisions, price_fn):
    """Score all decisions; aggregate good/bad/neutral counts by action."""
    outcomes = []
    skipped = 0
    by_action: dict = {}
    for d in decisions:
        o = score_decision(d, price_fn)
        if o is None:
            skipped += 1
            continue
        outcomes.append(o)
        g, b, n = by_action.get(d.action, (0, 0, 0))
        by_action[d.action] = (
            g + (o.verdict == "good"),
            b + (o.verdict == "bad"),
            n + (o.verdict == "neutral"),
        )
    return Scorecard(outcomes=outcomes, skipped=skipped, by_action=by_action)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_decision_review.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add app/decision_review.py tests/test_decision_review.py
git commit -m "feat: score_decision + build_scorecard"
```

---

### Task 3: `format_scorecard_prompt` + `format_scorecard_embed`

**Files:**
- Modify: `app/decision_review.py` (append)
- Test: `tests/test_decision_review.py` (append)

**Interfaces:**
- Consumes: `Scorecard`, `DecisionOutcome`, `Decision`.
- Produces: `format_scorecard_prompt(sc) -> str` (`""` when no outcomes); `format_scorecard_embed(sc) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_decision_review.py
from app.decision_review import format_scorecard_prompt, format_scorecard_embed, Scorecard, DecisionOutcome


def _oc(dt, tk, ac, rel, verdict):
    return DecisionOutcome(Decision(dt, tk, ac), rel, 0.0, rel, verdict)


def test_prompt_empty_when_no_outcomes():
    assert format_scorecard_prompt(Scorecard()) == ""


def test_prompt_text_is_deterministic():
    sc = Scorecard(
        outcomes=[_oc("2026-07-01", "AAA", "SELL", -0.1, "good")] * 4,
        skipped=0,
        by_action={"SELL": (2, 1, 1), "BUY": (1, 0, 0)},
    )
    text = format_scorecard_prompt(sc)
    assert text == (
        "Last 4 executed decisions scored vs SPY since each decision: "
        "BUY: 1/1 good; SELL: 2/4 good, 1 neutral. "
        "Calibrate: repeat what worked, reconsider what didn't."
    )


def test_embed_has_title_and_counts():
    sc = Scorecard(
        outcomes=[_oc("2026-07-01", "AAA", "SELL", -0.1, "good"),
                  _oc("2026-07-02", "BBB", "BUY", 0.05, "good")],
        skipped=1,
        by_action={"SELL": (1, 0, 0), "BUY": (1, 0, 0)},
    )
    embed = format_scorecard_embed(sc)
    assert embed["title"] == "📅 KIMI DECISION REVIEW"
    assert "Scored 2" in embed["description"]
    assert "1 skipped" in embed["description"]
    assert embed["fields"]  # has at least the aggregate field
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_decision_review.py::test_prompt_text_is_deterministic -v`
Expected: FAIL with `ImportError: cannot import name 'format_scorecard_prompt'`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/decision_review.py`. Note the `_ACTION_ORDER` ensures deterministic output; `format_scorecard_embed` imports `claude_manager` **lazily inside the function** to avoid the import cycle:

```python
_ACTION_ORDER = ("BUY", "DOUBLE_DOWN", "SELL", "TRIM")


def format_scorecard_prompt(sc) -> str:
    if not sc.outcomes:
        return ""
    parts = []
    for action in _ACTION_ORDER:
        if action in sc.by_action:
            g, b, n = sc.by_action[action]
            total = g + b + n
            piece = f"{action}: {g}/{total} good"
            if n:
                piece += f", {n} neutral"
            parts.append(piece)
    return (
        f"Last {len(sc.outcomes)} executed decisions scored vs SPY since each decision: "
        + "; ".join(parts)
        + ". Calibrate: repeat what worked, reconsider what didn't."
    )


def format_scorecard_embed(sc) -> dict:
    from app.claude_manager import _embed, _field, _CLR_YELLOW, _timestamp
    lines = []
    for action in _ACTION_ORDER:
        if action in sc.by_action:
            g, b, n = sc.by_action[action]
            line = f"**{action}** — {g} good / {b} bad"
            if n:
                line += f" / {n} neutral"
            lines.append(line)
    fields = [_field("By action", "\n".join(lines) or "—", inline=False)]
    recent = sc.outcomes[:5]
    if recent:
        rlines = [
            f"{o.decision.date} {o.decision.action} {o.decision.ticker}: "
            f"{o.rel * 100:+.1f}% vs SPY ({o.verdict})"
            for o in recent
        ]
        fields.append(_field("Most recent", "\n".join(rlines), inline=False))
    desc = f"Scored {len(sc.outcomes)} executed decision(s) vs SPY"
    if sc.skipped:
        desc += f"; {sc.skipped} skipped (no price data)"
    desc += "."
    return _embed("📅 KIMI DECISION REVIEW", _CLR_YELLOW, description=desc, fields=fields, footer=_timestamp())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_decision_review.py -v`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
git add app/decision_review.py tests/test_decision_review.py
git commit -m "feat: scorecard prompt + embed formatters"
```

---

### Task 4: yfinance adapter + `build_live_scorecard`

**Files:**
- Modify: `app/decision_review.py` (append)
- Test: `tests/test_decision_review.py` (append)

**Interfaces:**
- Consumes: `load_executed_decisions`, `build_scorecard`, `Scorecard`.
- Produces: `_yf_price_fn(ticker, start_date) -> tuple[float, float] | None`; `build_live_scorecard() -> Scorecard` (never raises).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_decision_review.py
import app.decision_review as dr


def test_build_live_scorecard_never_raises(monkeypatch):
    # If loading blows up, we must degrade to an empty scorecard, not raise.
    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(dr, "load_executed_decisions", boom)
    sc = dr.build_live_scorecard()
    assert sc.outcomes == [] and sc.skipped == 0 and sc.by_action == {}


def test_build_live_scorecard_uses_yf_adapter(monkeypatch):
    monkeypatch.setattr(dr, "load_executed_decisions",
                        lambda now=None: [dr.Decision("2026-06-01", "AAA", "SELL")])
    monkeypatch.setattr(dr, "_yf_price_fn",
                        lambda t, s: (100.0, 80.0) if t == "AAA" else (100.0, 100.0))
    sc = dr.build_live_scorecard()
    assert len(sc.outcomes) == 1 and sc.outcomes[0].verdict == "good"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_decision_review.py::test_build_live_scorecard_never_raises -v`
Expected: FAIL with `AttributeError: ... has no attribute 'build_live_scorecard'`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/decision_review.py`:

```python
def _yf_price_fn(ticker: str, start_date: str):
    """Return (first_close, latest_close) auto-adjusted since start_date, or None."""
    try:
        hist = yf.Ticker(ticker).history(start=start_date, auto_adjust=True)
        if hist is None or hist.empty:
            return None
        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return None
        return float(closes.iloc[0]), float(closes.iloc[-1])
    except Exception as exc:
        log.warning("decision_review price fetch failed for %s: %s", ticker, exc)
        return None


def build_live_scorecard():
    """Load decisions and score them with the live yfinance adapter. Never raises —
    a feedback-layer failure degrades to an empty scorecard."""
    try:
        return build_scorecard(load_executed_decisions(), _yf_price_fn)
    except Exception as exc:
        log.warning("build_live_scorecard failed, empty scorecard: %s", exc)
        return Scorecard()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_decision_review.py -v`
Expected: PASS (14 passed).

- [ ] **Step 5: Commit**

```bash
git add app/decision_review.py tests/test_decision_review.py
git commit -m "feat: yfinance price adapter + build_live_scorecard"
```

---

### Task 5: Wire into the monthly rebalance

**Files:**
- Modify: `app/claude_manager.py` — top imports, `_SYSTEM_PROMPT` (~line 137), `run_monthly_rebalance` (~lines 977–990).

**Interfaces:**
- Consumes: `build_live_scorecard`, `format_scorecard_prompt`, `format_scorecard_embed` (Tasks 3–4).

- [ ] **Step 1: Add the import**

Near the other `from app....` imports at the top of `app/claude_manager.py`, add:

```python
from app.decision_review import build_live_scorecard, format_scorecard_prompt, format_scorecard_embed
```

- [ ] **Step 2: Add the system-prompt line**

In `_SYSTEM_PROMPT`, immediately after the macro-context bullet (line 137, `- Use macro context...`), add:

```
- A "Decision Track Record (vs SPY)" section may be provided showing how your past executed trades fared against SPY since each decision — use it to calibrate: repeat what worked, reconsider what didn't.
```

- [ ] **Step 3: Build the scorecard, post the review embed, inject into the prompt**

In `run_monthly_rebalance`, immediately after `log_entry["spy_price_at_rebalance"] = spy_price` (line 979) and before the `# ── 3. Build prompt` comment, insert:

```python
        scorecard = await loop.run_in_executor(None, build_live_scorecard)
        scorecard_text = format_scorecard_prompt(scorecard)
        scorecard_block = f"Decision Track Record (vs SPY):\n{scorecard_text}\n\n" if scorecard_text else ""
        if scorecard.outcomes:
            await notify_claude_manager_embed(format_scorecard_embed(scorecard))
```

Then in the `prompt = (` string, add `f"{scorecard_block}"` on its own line immediately before the `f"Current Holdings:\n{json.dumps(enriched, indent=2)}\n\n"` line:

```python
            f"{history_text}\n\n"
            f"{scorecard_block}"
            f"Current Holdings:\n{json.dumps(enriched, indent=2)}\n\n"
```

- [ ] **Step 4: Run targeted + full suite**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_claude_manager_history.py tests/test_claude_manager_section_ticker.py tests/test_decision_review.py -v`
Expected: PASS.

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/ -q --ignore=tests/test_public_stats.py`
Expected: exactly the 8 pre-existing failures (`test_pnl.py`, `test_trade_notifier.py`), no others.

- [ ] **Step 5: Commit**

```bash
git add app/claude_manager.py
git commit -m "feat: inject decision scorecard + post monthly review in rebalance"
```

---

### Task 6: Wire into the weekly inspection

**Files:**
- Modify: `app/claude_inspection.py` — top imports, `_INSPECTION_SYSTEM_PROMPT` (~line 570), `run_weekly_inspection` (~lines 123–148).

**Interfaces:**
- Consumes: `build_live_scorecard`, `format_scorecard_prompt` (Tasks 3–4). (Inspection does NOT post the Discord embed.)

- [ ] **Step 1: Add the import**

Near the other imports at the top of `app/claude_inspection.py`, add:

```python
from app.decision_review import build_live_scorecard, format_scorecard_prompt
```

- [ ] **Step 2: Add the system-prompt line**

In `_INSPECTION_SYSTEM_PROMPT`, immediately after the line ending `...SPY is permanently excluded — never mention it.` (line 570), add a new paragraph:

```
A "Decision Track Record (vs SPY)" section may be provided showing how past executed trades fared against SPY since each decision — use it to calibrate: repeat what worked, reconsider what didn't.
```

- [ ] **Step 3: Build the scorecard and inject into the prompt**

In `run_weekly_inspection`, immediately after `data_gaps_by_ticker = annotate_and_collect_gaps(enriched)` and its gap-embed block (i.e. just before `log_entry["holdings_reviewed"] = ...` on line ~134, or before the `holdings_json = _json.dumps(...)` line), insert:

```python
        scorecard_text = format_scorecard_prompt(await loop.run_in_executor(None, build_live_scorecard))
        scorecard_block = f"Decision Track Record (vs SPY):\n{scorecard_text}\n\n" if scorecard_text else ""
```

Then in the `prompt = (` string, add `f"{scorecard_block}"` immediately before the `f"Current Holdings:\n{holdings_json}\n\n"` line:

```python
            f"Weekly Inspection — review current holdings for anything material since the last check-in.\n\n"
            f"{scorecard_block}"
            f"Current Holdings:\n{holdings_json}\n\n"
```

- [ ] **Step 4: Run targeted + full suite**

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/test_claude_inspection_run.py tests/test_claude_inspection_parse.py tests/test_claude_inspection_log.py tests/test_claude_inspection_thesis_map.py tests/test_decision_review.py -v`
Expected: PASS.

Run: `PYTHONPATH=. "/c/Users/moses/AppData/Local/Programs/Python/Python39/python.exe" -m pytest tests/ -q --ignore=tests/test_public_stats.py`
Expected: exactly the 8 pre-existing failures, no others.

- [ ] **Step 5: Commit**

```bash
git add app/claude_inspection.py
git commit -m "feat: inject decision scorecard into weekly inspection prompt"
```

---

## Self-Review

**Spec coverage:**
- Attribution method (rel, yfinance both legs, neutral band) → Task 2 `score_decision`. ✓
- Scope (executed only, 183d window, 20 cap, both logs) → Task 1 `load_executed_decisions`. ✓
- Skipped-on-no-data + never-guess → Task 2 (`score_decision` None) + Task 4 (`build_live_scorecard` try/except). ✓
- New module, no import cycle → `decision_review.py` with lazy `claude_manager` import only in `format_scorecard_embed`. ✓
- Prompt injection both systems + system-prompt line → Tasks 5 (Steps 2–3), 6 (Steps 2–3). ✓
- Monthly Discord review, rebalance only → Task 5 Step 3 (`if scorecard.outcomes`); inspection has none (Task 6). ✓
- Empty scorecard → no injection / no embed → `format_scorecard_prompt` returns `""`; embed guarded by `if scorecard.outcomes`. ✓
- Never raises into a run → `build_live_scorecard` try/except. ✓
- Testing per spec → Tasks 1–4 cover load/score/build/format/skip/error cases; wiring via existing suites. ✓

**Placeholder scan:** No TBD/TODO/vague steps; every code step shows complete code. ✓

**Type consistency:** `Decision(date,ticker,action)`, `DecisionOutcome(decision,stock_return,spy_return,rel,verdict)`, `Scorecard(outcomes,skipped,by_action)`, `price_fn(ticker,start_date)->tuple|None`, `by_action[action]=(good,bad,neutral)` used identically across Tasks 1–6. ✓
