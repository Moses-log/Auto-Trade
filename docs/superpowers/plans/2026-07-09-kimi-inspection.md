# Kimi Inspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a weekly "Kimi Inspection" pass over current holdings (SELL/TRIM/DOUBLE_DOWN authority, no BUY), fix a scheduling bug in the monthly rebalance it depends on, and wire the two together so each can see the other's recent decisions.

**Architecture:** A new `app/claude_inspection.py` module mirrors the shape of `run_monthly_rebalance()` in `app/claude_manager.py` but scoped to current holdings only, with a lighter/cheaper Claude call (smaller turn cap, smaller web-search budget, delta-check against the last known thesis instead of a full 5-section rebuild). It reuses existing helpers from `claude_manager.py` (`_fetch_yf_data`, `_fetch_technical_data`, `_embed`, `_field`, `_trade_embed`, `_parse_trade_block`, `_timestamp`) and existing trade-execution/notification functions (`rh_client`, `app.claude_portfolio`, `app.notifications`) rather than duplicating them. A new shared `is_first_trading_day_of()` helper in `app/trading/alpaca_client.py` generalizes a pattern already used by the quarterly tax report, and fixes a latent bug where the monthly rebalance silently skips a month when the 1st falls on a weekend/holiday.

**Tech Stack:** Python 3, FastAPI, APScheduler, `robin_stocks`, `alpaca-py`, Anthropic Messages API via `httpx`, pytest + `pytest-asyncio`.

## Global Constraints

- Inspection must never execute a `BUY` — this is enforced at the parsing/execution boundary in code, not just in the prompt (spec Section 4, item 7).
- Inspection runs only after regular market open (9:35 AM ET, same time-of-day as the monthly rebalance) — no premarket/extended-hours order logic is introduced.
- Inspection is skipped entirely for any week where the first trading day of that week is also the first trading day of the month (the monthly rebalance owns that week).
- Position-sizing constraints inherited from the monthly rebalance apply unchanged: 25% max position, no sector above 50%, a `DOUBLE_DOWN` pushing a position above 10% requires a resolved bear case documented in the reasoning.
- Inspection writes to its own `claude_inspection_log.json`, kept separate from `claude_rebalance_log.json` (spec Section 6).
- All new async code follows the existing "never raises" convention used throughout `robinhood_client.py` (catch, log, return a status dict).

---

### Task 1: Shared `is_first_trading_day_of()` helper

**Files:**
- Modify: `app/trading/alpaca_client.py:318-333` (insert after `was_market_open_today()`, before `get_next_trading_day()`)
- Test: Create `tests/test_alpaca_client_calendar.py`

**Interfaces:**
- Produces: `is_first_trading_day_of(period_start: date) -> bool` — importable as `from app.trading.alpaca_client import is_first_trading_day_of`. Used by Task 2 (monthly rebalance) and Task 4 (weekly inspection).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_alpaca_client_calendar.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest


@patch("app.trading.alpaca_client.date")
def test_true_when_period_start_is_today_or_future(mock_date):
    from app.trading.alpaca_client import is_first_trading_day_of
    mock_date.today.return_value = date(2026, 7, 1)
    assert is_first_trading_day_of(date(2026, 7, 1)) is True
    assert is_first_trading_day_of(date(2026, 7, 5)) is True


@patch("app.trading.alpaca_client.get_client")
@patch("app.trading.alpaca_client.date")
def test_true_when_no_prior_trading_day_in_range(mock_date, mock_get_client):
    from app.trading.alpaca_client import is_first_trading_day_of
    mock_date.today.return_value = date(2026, 7, 3)  # e.g. Jul 1-2 were a holiday weekend
    mock_client = MagicMock()
    mock_client.get_calendar.return_value = []
    mock_get_client.return_value = mock_client
    assert is_first_trading_day_of(date(2026, 7, 1)) is True


@patch("app.trading.alpaca_client.get_client")
@patch("app.trading.alpaca_client.date")
def test_false_when_a_prior_trading_day_exists(mock_date, mock_get_client):
    from app.trading.alpaca_client import is_first_trading_day_of
    mock_date.today.return_value = date(2026, 7, 3)
    mock_client = MagicMock()
    mock_client.get_calendar.return_value = [MagicMock()]  # Jul 1st or 2nd already traded
    mock_get_client.return_value = mock_client
    assert is_first_trading_day_of(date(2026, 7, 1)) is False


@patch("app.trading.alpaca_client.get_client")
@patch("app.trading.alpaca_client.date")
def test_defaults_true_on_error(mock_date, mock_get_client):
    from app.trading.alpaca_client import is_first_trading_day_of
    mock_date.today.return_value = date(2026, 7, 3)
    mock_get_client.side_effect = Exception("API down")
    assert is_first_trading_day_of(date(2026, 7, 1)) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_alpaca_client_calendar.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_first_trading_day_of'`

- [ ] **Step 3: Implement `is_first_trading_day_of()`**

In `app/trading/alpaca_client.py`, insert immediately after `was_market_open_today()` (after line 332, before `def get_next_trading_day()` at line 335):

```python
def is_first_trading_day_of(period_start: date) -> bool:
    """Return True if no trading day occurred between period_start and yesterday (inclusive).

    Used when a cron job fires on a fixed calendar day/window that might
    start on a holiday or weekend: cover the window with a few days of cron
    (e.g. day="1-3"), then call this inside the job to skip once an earlier
    day in the period already ran. Defaults to True on error — same
    fail-open behavior as was_market_open_today().
    """
    today = date.today()
    if period_start >= today:
        return True
    try:
        req = GetCalendarRequest(start=period_start, end=today - timedelta(days=1))
        prior = get_client().get_calendar(req)
        return not prior
    except Exception as exc:
        log.warning(
            "Could not verify first trading day for period starting %s: %s",
            period_start, exc,
        )
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_alpaca_client_calendar.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/trading/alpaca_client.py tests/test_alpaca_client_calendar.py
git commit -m "feat: add is_first_trading_day_of helper for cron holiday guards"
```

---

### Task 2: Fix monthly rebalance scheduling bug

**Files:**
- Modify: `app/scheduler.py:153-155` (`_claude_monthly_rebalance`), `app/scheduler.py:189-194` (cron registration), `app/scheduler.py:22` (import line)
- Test: Create `tests/test_scheduler_rebalance_guard.py`

**Interfaces:**
- Consumes: `is_first_trading_day_of(period_start: date) -> bool` (Task 1), `was_market_open_today() -> bool` (existing)
- Produces: `_claude_monthly_rebalance()` — same name, now guarded; no external consumers change.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scheduler_rebalance_guard.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@patch("app.scheduler.run_monthly_rebalance", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of", return_value=True)
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_rebalance_runs_when_first_trading_day(
    mock_date, mock_was_open, mock_is_first, mock_rebalance,
):
    from app.scheduler import _claude_monthly_rebalance
    mock_date.today.return_value = date(2026, 7, 1)

    await _claude_monthly_rebalance()

    mock_rebalance.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.scheduler.run_monthly_rebalance", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of", return_value=False)
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_rebalance_skips_when_earlier_day_already_ran(
    mock_date, mock_was_open, mock_is_first, mock_rebalance,
):
    """Cron fired on day 3, but day 1 (a Saturday's Monday makeup) already ran."""
    from app.scheduler import _claude_monthly_rebalance
    mock_date.today.return_value = date(2026, 7, 3)

    await _claude_monthly_rebalance()

    mock_rebalance.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.scheduler.run_monthly_rebalance", new_callable=AsyncMock)
@patch("app.scheduler.was_market_open_today", return_value=False)
@patch("app.scheduler._date")
async def test_rebalance_skips_on_holiday(mock_date, mock_was_open, mock_rebalance):
    from app.scheduler import _claude_monthly_rebalance
    mock_date.today.return_value = date(2026, 7, 1)

    await _claude_monthly_rebalance()

    mock_rebalance.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scheduler_rebalance_guard.py -v`
Expected: FAIL — `_claude_monthly_rebalance` currently calls `run_monthly_rebalance` unconditionally, so `test_rebalance_skips_when_earlier_day_already_ran` and `test_rebalance_skips_on_holiday` fail because the mock IS awaited. Also `is_first_trading_day_of`/`run_monthly_rebalance` aren't importable as module attributes of `app.scheduler` yet, so all three fail with `AttributeError` on the `@patch` target.

- [ ] **Step 3: Implement the guard**

In `app/scheduler.py`, change the import line 22 from:

```python
from app.trading.alpaca_client import was_market_open_today, get_client
```

to:

```python
from app.trading.alpaca_client import was_market_open_today, get_client, is_first_trading_day_of
```

Replace `_claude_monthly_rebalance()` (lines 153-155):

```python
async def _claude_monthly_rebalance() -> None:
    from app.claude_manager import run_monthly_rebalance
    await run_monthly_rebalance()
```

with:

```python
async def _claude_monthly_rebalance() -> None:
    """Fires on the first trading day of the month (cron covers days 1-3 to
    handle cases where the 1st is a holiday or weekend — same pattern as
    _quarterly_tax_report)."""
    from app.claude_manager import run_monthly_rebalance
    if not was_market_open_today():
        log.info("_claude_monthly_rebalance: market holiday — skipping")
        return
    if not is_first_trading_day_of(_date.today().replace(day=1)):
        log.info("_claude_monthly_rebalance: not first trading day of month — skipping")
        return
    await run_monthly_rebalance()
```

Change the cron registration (lines 189-194) from:

```python
    scheduler.add_job(
        _claude_monthly_rebalance,
        CronTrigger(day=1, hour=9, minute=35, timezone=ET),
        id="claude_monthly_rebalance",
        replace_existing=True,
    )
```

to:

```python
    scheduler.add_job(
        _claude_monthly_rebalance,
        CronTrigger(day="1-3", hour=9, minute=35, timezone=ET),
        id="claude_monthly_rebalance",
        replace_existing=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scheduler_rebalance_guard.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the full existing scheduler test suite to check for regressions**

Run: `pytest tests/test_scheduler_equity_catchup.py tests/test_scheduler_withdrawals.py -v`
Expected: all pass unchanged (this task doesn't touch those code paths)

- [ ] **Step 6: Commit**

```bash
git add app/scheduler.py tests/test_scheduler_rebalance_guard.py
git commit -m "fix: monthly rebalance no longer silently skips a month when the 1st is a holiday"
```

---

### Task 3: `app/claude_inspection.py` module skeleton + inspection log I/O

**Files:**
- Create: `app/claude_inspection.py`
- Test: Create `tests/test_claude_inspection_log.py`

**Interfaces:**
- Produces: `_INSPECTION_LOG_PATH: str`, `_append_inspection_log(entry: dict) -> None`, `_load_recent_inspection_entries(limit: int = 5) -> list[dict]`. Used by Task 6-9.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_inspection_log.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

import json
from unittest.mock import patch


def test_append_and_load_round_trip(tmp_path):
    log_path = tmp_path / "claude_inspection_log.json"
    with patch("app.claude_inspection._INSPECTION_LOG_PATH", str(log_path)):
        from app.claude_inspection import _append_inspection_log, _load_recent_inspection_entries

        _append_inspection_log({"timestamp": "2026-07-06T09:35:00", "status": "completed"})
        _append_inspection_log({"timestamp": "2026-07-13T09:35:00", "status": "no_changes"})

        entries = _load_recent_inspection_entries()
        assert len(entries) == 2
        assert entries[-1]["status"] == "no_changes"


def test_load_returns_empty_list_when_file_missing(tmp_path):
    log_path = tmp_path / "does_not_exist.json"
    with patch("app.claude_inspection._INSPECTION_LOG_PATH", str(log_path)):
        from app.claude_inspection import _load_recent_inspection_entries
        assert _load_recent_inspection_entries() == []


def test_load_respects_limit(tmp_path):
    log_path = tmp_path / "claude_inspection_log.json"
    with patch("app.claude_inspection._INSPECTION_LOG_PATH", str(log_path)):
        from app.claude_inspection import _append_inspection_log, _load_recent_inspection_entries
        for i in range(10):
            _append_inspection_log({"timestamp": f"2026-0{i%9+1}-01T09:35:00", "status": "completed"})
        entries = _load_recent_inspection_entries(limit=3)
        assert len(entries) == 3


def test_append_caps_history_at_36_entries(tmp_path):
    log_path = tmp_path / "claude_inspection_log.json"
    with patch("app.claude_inspection._INSPECTION_LOG_PATH", str(log_path)):
        from app.claude_inspection import _append_inspection_log
        for i in range(40):
            _append_inspection_log({"timestamp": f"entry-{i}", "status": "completed"})
        with open(log_path) as f:
            records = json.load(f)
        assert len(records) == 36
        assert records[-1]["timestamp"] == "entry-39"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claude_inspection_log.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.claude_inspection'`

- [ ] **Step 3: Create `app/claude_inspection.py`**

```python
"""
claude_inspection.py — Weekly Kimi Inspection: a lightweight, holdings-only
review that runs on the first trading day of the week (skipped when it
coincides with the monthly rebalance), with authority to SELL, TRIM, or
DOUBLE_DOWN — never BUY. See docs/superpowers/specs/2026-07-09-kimi-inspection-design.md.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

_INSPECTION_LOG_PATH = os.getenv("CLAUDE_INSPECTION_LOG_PATH", "/data/claude_inspection_log.json")


def _append_inspection_log(entry: dict) -> None:
    try:
        try:
            with open(_INSPECTION_LOG_PATH) as f:
                records = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            records = []
        records.append(entry)
        if len(records) > 36:          # cap at ~3 years of weekly logs (52/yr, generous)
            records = records[-36:]
        with open(_INSPECTION_LOG_PATH, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as exc:
        log.warning("Failed to write inspection log: %s", exc)


def _load_recent_inspection_entries(limit: int = 5) -> list[dict]:
    try:
        with open(_INSPECTION_LOG_PATH) as f:
            records = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return records[-limit:]


async def run_weekly_inspection() -> None:
    """Placeholder — replaced with the real implementation in Task 8/9.

    Exists now so app/scheduler.py (Task 4) can import this name at module
    level; @patch("app.scheduler.run_weekly_inspection", ...) requires the
    attribute to already exist (patch's default create=False), which in turn
    requires app.claude_inspection.run_weekly_inspection to exist. Task 8
    replaces this entire function body — not append a second definition.
    """
    raise NotImplementedError("run_weekly_inspection is implemented in Task 8/9")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claude_inspection_log.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/claude_inspection.py tests/test_claude_inspection_log.py
git commit -m "feat: add claude_inspection module skeleton with log read/write"
```

---

### Task 4: Weekly inspection scheduler wiring + guard

**Files:**
- Modify: `app/scheduler.py:163-200` (`setup_jobs`), insert new `_weekly_inspection()` function near `_claude_monthly_rebalance`
- Test: Create `tests/test_scheduler_inspection_guard.py`

**Interfaces:**
- Consumes: `is_first_trading_day_of()` (Task 1), `run_weekly_inspection()` (a `NotImplementedError` placeholder from Task 3, fully implemented in Tasks 8-9). Import it at **module level** in `app/scheduler.py`, not lazily inside the function — this task's tests use `@patch("app.scheduler.run_weekly_inspection", ...)`, and `unittest.mock.patch` requires the target to already exist as a module attribute (default `create=False`). Task 2 hit this exact issue with `run_monthly_rebalance` and fixed it the same way by hoisting to a module-level import — follow that same fix here from the start.
- Produces: `_weekly_inspection()` — new scheduler entry point; cron job id `"weekly_inspection"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scheduler_inspection_guard.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@patch("app.scheduler.run_weekly_inspection", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of")
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_inspection_runs_on_first_trading_day_of_week(
    mock_date, mock_was_open, mock_is_first, mock_inspection,
):
    from app.scheduler import _weekly_inspection
    monday = date(2026, 7, 6)  # a Monday, not the 1st of a month
    mock_date.today.return_value = monday
    # is_first_trading_day_of(week_start) -> True (first trading day of week)
    # is_first_trading_day_of(month_start) -> False (not the rebalance day)
    mock_is_first.side_effect = [True, False]

    await _weekly_inspection()

    mock_inspection.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.scheduler.run_weekly_inspection", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of")
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_inspection_skips_when_already_ran_this_week(
    mock_date, mock_was_open, mock_is_first, mock_inspection,
):
    from app.scheduler import _weekly_inspection
    wednesday = date(2026, 7, 8)
    mock_date.today.return_value = wednesday
    mock_is_first.return_value = False  # Monday or Tuesday already traded this week

    await _weekly_inspection()

    mock_inspection.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.scheduler.run_weekly_inspection", new_callable=AsyncMock)
@patch("app.scheduler.is_first_trading_day_of")
@patch("app.scheduler.was_market_open_today", return_value=True)
@patch("app.scheduler._date")
async def test_inspection_skips_when_coincides_with_monthly_rebalance(
    mock_date, mock_was_open, mock_is_first, mock_inspection,
):
    from app.scheduler import _weekly_inspection
    first_of_month = date(2026, 9, 1)  # a Tuesday that's also the 1st
    mock_date.today.return_value = first_of_month
    # first trading day of week -> True, but also first trading day of month -> True
    mock_is_first.side_effect = [True, True]

    await _weekly_inspection()

    mock_inspection.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.scheduler.run_weekly_inspection", new_callable=AsyncMock)
@patch("app.scheduler.was_market_open_today", return_value=False)
@patch("app.scheduler._date")
async def test_inspection_skips_on_holiday(mock_date, mock_was_open, mock_inspection):
    from app.scheduler import _weekly_inspection
    mock_date.today.return_value = date(2026, 7, 6)

    await _weekly_inspection()

    mock_inspection.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scheduler_inspection_guard.py -v`
Expected: FAIL — `_weekly_inspection` doesn't exist yet (`AttributeError`/`ImportError`)

- [ ] **Step 3: Implement `_weekly_inspection()` and register the cron job**

In `app/scheduler.py`, add `run_weekly_inspection` to the module-level imports (alongside wherever `run_monthly_rebalance` ended up after Task 2 — e.g. `from app.claude_manager import run_monthly_rebalance` becomes a two-line block also importing `from app.claude_inspection import run_weekly_inspection`, or one combined import — match whatever form Task 2 actually left in the file). Then insert immediately after `_claude_monthly_rebalance()` (after the code from Task 2):

```python
async def _weekly_inspection() -> None:
    """Fires on the first trading day of the week (cron covers Mon-Wed to
    handle a Monday/Tuesday holiday). Skipped when today is also the first
    trading day of the month — the monthly rebalance owns that week."""
    if not was_market_open_today():
        log.info("_weekly_inspection: market holiday — skipping")
        return
    today = _date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday of this week
    if not is_first_trading_day_of(week_start):
        log.info("_weekly_inspection: not first trading day of week — skipping")
        return
    if is_first_trading_day_of(today.replace(day=1)):
        log.info("_weekly_inspection: coincides with monthly rebalance day — skipping")
        return
    await run_weekly_inspection()
```

In `setup_jobs()`, add a new job registration immediately after the `claude_monthly_rebalance` block (after line 194, before the `_nightly_backup` block):

```python
    scheduler.add_job(
        _weekly_inspection,
        CronTrigger(day_of_week="mon-wed", hour=9, minute=35, timezone=ET),
        id="weekly_inspection",
        replace_existing=True,
    )
```

Update the `log.info(...)` summary string at the end of `setup_jobs()` (lines 201-207) to mention the new job — append `", weekly_inspection (first trading day of week, 9:35 AM ET, skipped on rebalance weeks)"` to the existing message.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scheduler_inspection_guard.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the full scheduler test suite for regressions**

Run: `pytest tests/test_scheduler_equity_catchup.py tests/test_scheduler_withdrawals.py tests/test_scheduler_rebalance_guard.py tests/test_scheduler_inspection_guard.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add app/scheduler.py tests/test_scheduler_inspection_guard.py
git commit -m "feat: register weekly Kimi Inspection cron job with holiday/rebalance-collision guards"
```

---

### Task 5: Hoist `_DIVIDER`/`_section_ticker` to module level in `claude_manager.py`

**Files:**
- Modify: `app/claude_manager.py:80-97` (insert after `_trade_embed`), `app/claude_manager.py:984-994` (remove the now-duplicate local definitions inside `run_monthly_rebalance`)
- Test: Create `tests/test_claude_manager_section_ticker.py`

**Interfaces:**
- Produces: module-level `_DIVIDER: str` and `_section_ticker(section: str) -> str` on `app.claude_manager`, importable by `app.claude_inspection` (Task 6).

This is a pure hoist — no behavior change inside `run_monthly_rebalance` itself, since the removed nested `def`/assignment resolve to the same module-level names once moved. `claude_manager.py` currently has zero test coverage; this task adds the first tests for it, scoped to exactly what's being touched.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_manager_section_ticker.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("RH_USERNAME", "test@example.com")
os.environ.setdefault("RH_PASSWORD", "test_password")


def test_section_ticker_extracts_symbol_with_em_dash():
    from app.claude_manager import _section_ticker
    section = "## NVDA — NVIDIA Corp\nCurrent: 8%  →  Target: 10%\n"
    assert _section_ticker(section) == "NVDA"


def test_section_ticker_extracts_symbol_with_en_dash():
    from app.claude_manager import _section_ticker
    section = "## META – Meta Platforms\n"
    assert _section_ticker(section) == "META"


def test_section_ticker_returns_empty_when_no_header():
    from app.claude_manager import _section_ticker
    assert _section_ticker("no header here") == ""


def test_divider_is_module_level_string():
    from app.claude_manager import _DIVIDER
    assert isinstance(_DIVIDER, str) and len(_DIVIDER) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claude_manager_section_ticker.py -v`
Expected: FAIL — `_section_ticker`/`_DIVIDER` are currently local to `run_monthly_rebalance`, not importable from the module.

- [ ] **Step 3: Hoist the definitions**

In `app/claude_manager.py`, insert immediately after `_trade_embed()` (after line 96, before `_SYSTEM_PROMPT = """..."""` at line 98):

```python
_DIVIDER = "══════════════════════════════"


def _section_ticker(section: str) -> str:
    """Extract the ticker symbol from a '## TICKER — Name' header line."""
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("## "):
            first_word = line[3:].split("—")[0].split("–")[0].strip().split()[0]
            return first_word.upper()
    return ""
```

Then, inside `run_monthly_rebalance()` (around what was lines 984-994), delete the now-redundant local definitions:

```python
        from app.notifications import notify_claude_signal_feed
        _DIVIDER = "══════════════════════════════"

        def _section_ticker(section: str) -> str:
            """Extract the ticker symbol from a '## TICKER — Name' header line."""
            for line in section.splitlines():
                line = line.strip()
                if line.startswith("## "):
                    first_word = line[3:].split("—")[0].split("–")[0].strip().split()[0]
                    return first_word.upper()
            return ""
```

becomes:

```python
        from app.notifications import notify_claude_signal_feed
```

(the rest of the function is unchanged — `_DIVIDER` and `_section_ticker(section)` used later in the function now resolve to the module-level definitions instead of the local ones, with identical behavior).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claude_manager_section_ticker.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/claude_manager.py tests/test_claude_manager_section_ticker.py
git commit -m "refactor: hoist _DIVIDER/_section_ticker to module level for reuse by Kimi Inspection"
```

---

### Task 6: Inspection system prompt, lighter Claude call, and BUY-rejecting parser

**Files:**
- Modify: `app/claude_inspection.py` (append)
- Test: Create `tests/test_claude_inspection_parse.py`

**Interfaces:**
- Consumes: `_parse_trade_block(text: str) -> Optional[dict]` from `app.claude_manager` (existing, generic JSON-block extractor).
- Produces: `_INSPECTION_SYSTEM_PROMPT: str`, `_call_claude_inspection_sync(user_message: str) -> str`, `_parse_inspection_trade_block(text: str) -> Optional[dict]`. Used by Task 7.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_inspection_parse.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — this file doesn't need Robinhood
# credentials (it only tests prompt/parser logic), and Task 5 hit a real
# regression where a "test_cla..." file setting these via setdefault() sorted
# alphabetically before tests/test_config_rh.py and polluted its
# settings.rh_username/rh_password-is-None assertions for the whole pytest run.


def test_parse_accepts_hold_sell_trim_double_down():
    from app.claude_inspection import _parse_inspection_trade_block
    text = '''Some analysis text.
```json
{
  "no_changes": false,
  "trades": [
    {"action": "HOLD", "ticker": "MSFT"},
    {"action": "SELL", "ticker": "NOW"},
    {"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 8},
    {"action": "DOUBLE_DOWN", "ticker": "META", "target_weight_pct": 22}
  ]
}
```'''
    result = _parse_inspection_trade_block(text)
    assert result is not None
    actions = {t["action"] for t in result["trades"]}
    assert actions == {"HOLD", "SELL", "TRIM", "DOUBLE_DOWN"}


def test_parse_rejects_buy_action():
    from app.claude_inspection import _parse_inspection_trade_block
    text = '''```json
{
  "no_changes": false,
  "trades": [
    {"action": "BUY", "ticker": "FICO", "target_weight_pct": 10}
  ]
}
```'''
    result = _parse_inspection_trade_block(text)
    assert result is None


def test_parse_rejects_mixed_block_containing_any_buy():
    """A block with one legitimate TRIM and one disallowed BUY is rejected wholesale
    rather than silently dropping the BUY and executing the rest — a model that
    proposes a BUY during Inspection indicates a prompt/constraint failure worth
    surfacing loudly, not papering over."""
    from app.claude_inspection import _parse_inspection_trade_block
    text = '''```json
{
  "no_changes": false,
  "trades": [
    {"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 8},
    {"action": "BUY", "ticker": "FICO", "target_weight_pct": 10}
  ]
}
```'''
    result = _parse_inspection_trade_block(text)
    assert result is None


def test_parse_returns_none_when_no_json_block():
    from app.claude_inspection import _parse_inspection_trade_block
    assert _parse_inspection_trade_block("no json here") is None


def test_search_tool_has_reduced_budget():
    from app.claude_inspection import _INSPECTION_WEB_SEARCH_TOOL
    assert _INSPECTION_WEB_SEARCH_TOOL["max_uses"] <= 15
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claude_inspection_parse.py -v`
Expected: FAIL — `_parse_inspection_trade_block`/`_INSPECTION_WEB_SEARCH_TOOL` don't exist yet.

- [ ] **Step 3: Implement the prompt, lighter agentic loop, and BUY-rejecting parser**

Append to `app/claude_inspection.py`:

```python
import httpx

from app.claude_manager import _parse_trade_block
from app.config import settings

_INSPECTION_WEB_SEARCH_TOOL: dict = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 15,
}

_INSPECTION_SYSTEM_PROMPT = """You are Kimi Inspection, a weekly holdings-only check-in that runs \
between Kimi Portfolio Manager's monthly rebalances.

Your job is narrower than a full rebalance: for each current holding, decide whether anything \
material has happened in the last 7 days that changes the existing thesis. You are NOT re-deriving \
each thesis from scratch — you are given the most recent thesis for each ticker and asked whether \
it still holds.

DEFAULT TO HOLD. Only recommend action (SELL, TRIM, or DOUBLE_DOWN) when there is a specific, \
nameable trigger:
- An earnings surprise (beat or miss) since the last review
- A guidance change (raised or cut)
- Major company-specific news (management change, regulatory action, product failure, M&A)
- A macro/sector shock clearly tied to this specific name
- A meaningful technical breakdown (a major support level broken with volume, a fresh death cross)

Routine day-to-day price noise is NOT a trigger. If nothing material happened for a holding, the \
correct action is HOLD — do not manufacture a reason to trade.

HARD RULE: You may never propose BUY. You only act on tickers already held. New positions are \
opened exclusively by the monthly rebalance's candidate screening — that is out of scope here.

Position-sizing constraints (same as the monthly rebalance): maximum position size 25%, no single \
sector above 50%, and a DOUBLE_DOWN that would push a position above 10% requires you to explicitly \
state why the existing bear case is still resolved. SPY is permanently excluded — never mention it.

REQUIRED OUTPUT FORMAT: end your response with a JSON block in exactly this format:

```json
{
  "no_changes": false,
  "trades": [
    {"action": "HOLD", "ticker": "MSFT"},
    {"action": "SELL", "ticker": "NOW"},
    {"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 8},
    {"action": "DOUBLE_DOWN", "ticker": "META", "target_weight_pct": 22}
  ]
}
```

Rules for the JSON block:
- Set "no_changes": true if no holding needs any action this week.
- action must be exactly "HOLD", "SELL", "TRIM", or "DOUBLE_DOWN" — never "BUY".
- Every current holding must appear exactly once in "trades".
- target_weight_pct is required for TRIM and DOUBLE_DOWN; omit for SELL and HOLD.
- Do not include markdown, comments, or extra fields in the JSON block."""


def _call_claude_inspection_sync(user_message: str) -> str:
    """Agentic loop with live web search, sized for a weekly holdings-only check.

    Same shape as claude_manager._call_claude_sync but with a smaller turn cap
    (30 vs 80) and a smaller web-search budget (15 vs 30 uses), since Inspection
    only does a delta-check against the last known thesis, not a full rebuild.
    """
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05",
        "content-type": "application/json",
    }
    messages: list[dict] = [{"role": "user", "content": user_message}]
    for _turn in range(30):
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": "claude-opus-4-8",
                "max_tokens": 8000,
                "system": _INSPECTION_SYSTEM_PROMPT,
                "messages": messages,
                "tools": [_INSPECTION_WEB_SEARCH_TOOL],
            },
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        content: list = data["content"]
        stop_reason: str = data.get("stop_reason", "end_turn")
        messages.append({"role": "assistant", "content": content})
        if stop_reason == "end_turn":
            return "\n".join(b["text"] for b in content if b.get("type") == "text")
        if stop_reason == "max_tokens":
            log.error("Inspection call hit max_tokens limit — response may be truncated")
            return "\n".join(b["text"] for b in content if b.get("type") == "text")
        if stop_reason == "tool_use":
            resolved_ids = {b["tool_use_id"] for b in content if b.get("tool_use_id")}
            pending = [b for b in content if b.get("type") == "tool_use" and b.get("id") not in resolved_ids]
            if pending:
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": b["id"], "content": ""} for b in pending],
                })
            continue
        break
    log.warning("Inspection agentic loop hit 30-turn safety cap — returning last assistant turn only")
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            texts = [
                b["text"] for b in (msg["content"] if isinstance(msg["content"], list) else [])
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


def _parse_inspection_trade_block(text: str) -> "dict | None":
    """Parse the trade block and reject it wholesale if it contains a BUY.

    Inspection must never open a new position — this is enforced here, not
    just in the prompt. A BUY appearing anywhere in the block indicates a
    prompt/constraint failure worth surfacing loudly (the caller logs and
    skips execution for the whole run) rather than silently dropping just
    the BUY and executing the rest.
    """
    block = _parse_trade_block(text)
    if block is None:
        return None
    trades = block.get("trades", [])
    if any(t.get("action") == "BUY" for t in trades):
        log.error("Inspection proposed a BUY action — rejecting entire trade block: %s", trades)
        return None
    return block
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claude_inspection_parse.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/claude_inspection.py tests/test_claude_inspection_parse.py
git commit -m "feat: add Inspection system prompt, lighter agentic loop, and BUY-rejecting parser"
```

---

### Task 7: Prior-thesis map (delta-check input)

**Files:**
- Modify: `app/claude_inspection.py` (append)
- Test: Create `tests/test_claude_inspection_thesis_map.py`

**Interfaces:**
- Consumes: `_DIVIDER`, `_section_ticker()` from `app.claude_manager` (Task 5); `_load_recent_inspection_entries()` (Task 3).
- Produces: `_build_prior_thesis_map(rebalance_records: list[dict], inspection_records: list[dict]) -> dict[str, str]`. Used by Task 8.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_inspection_thesis_map.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — see Task 6's note on the
# test_config_rh.py alphabetical-collision regression found in Task 5.


def test_extracts_per_ticker_thesis_from_last_rebalance():
    from app.claude_inspection import _build_prior_thesis_map
    divider = "══════════════════════════════"
    analysis_body = (
        f"{divider}\n## NVDA — NVIDIA Corp\nStrong AI infra moat.\n"
        f"{divider}\n## META — Meta Platforms\nAd business reaccelerating.\n"
    )
    rebalance_records = [{"timestamp": "2026-07-01T09:35:00", "analysis_body": analysis_body}]
    result = _build_prior_thesis_map(rebalance_records, [])
    assert "NVDA" in result and "Strong AI infra moat." in result["NVDA"]
    assert "META" in result and "reaccelerating" in result["META"]


def test_returns_empty_map_when_no_rebalance_history():
    from app.claude_inspection import _build_prior_thesis_map
    assert _build_prior_thesis_map([], []) == {}


def test_inspection_entry_overrides_older_rebalance_thesis():
    from app.claude_inspection import _build_prior_thesis_map
    divider = "══════════════════════════════"
    rebalance_records = [{
        "timestamp": "2026-07-01T09:35:00",
        "analysis_body": f"{divider}\n## NVDA — NVIDIA Corp\nOriginal monthly thesis.\n",
    }]
    inspection_records = [{
        "timestamp": "2026-07-13T09:35:00",
        "notes": {"NVDA": "Updated after Inspection: earnings beat, raising conviction."},
    }]
    result = _build_prior_thesis_map(rebalance_records, inspection_records)
    assert "raising conviction" in result["NVDA"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claude_inspection_thesis_map.py -v`
Expected: FAIL — `_build_prior_thesis_map` doesn't exist yet.

- [ ] **Step 3: Implement `_build_prior_thesis_map()`**

Append to `app/claude_inspection.py`:

```python
from app.claude_manager import _DIVIDER, _section_ticker


def _build_prior_thesis_map(rebalance_records: list, inspection_records: list) -> dict:
    """Build {ticker: most_recent_thesis_text}, sourced from the last rebalance's
    per-ticker research sections, then overlaid with any more recent Inspection
    notes (an Inspection that ran after the last rebalance has a fresher view)."""
    thesis_map: dict = {}

    if rebalance_records:
        last = rebalance_records[-1]
        analysis_body = last.get("analysis_body") or ""
        for section in analysis_body.split(_DIVIDER):
            section = section.strip()
            if not section:
                continue
            ticker = _section_ticker(section)
            if ticker:
                thesis_map[ticker] = section

    for entry in inspection_records:
        for ticker, note in (entry.get("notes") or {}).items():
            thesis_map[ticker.upper()] = note

    return thesis_map
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claude_inspection_thesis_map.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/claude_inspection.py tests/test_claude_inspection_thesis_map.py
git commit -m "feat: build per-ticker prior-thesis map from rebalance + inspection history"
```

---

### Task 8: `run_weekly_inspection()` — data gathering through Claude call

**Files:**
- Modify: `app/claude_inspection.py` (append)
- Test: Create `tests/test_claude_inspection_run.py`

**Interfaces:**
- Consumes: `rh_client` (`app.trading.robinhood_client`), `_fetch_yf_data`/`_fetch_technical_data`/`_embed`/`_timestamp` (`app.claude_manager`), `notify_claude_manager_embed` (`app.notifications`), `_call_claude_inspection_sync`/`_parse_inspection_trade_block`/`_build_prior_thesis_map`/`_load_recent_inspection_entries`/`_append_inspection_log` (this module, Tasks 3/6/7).
- Produces: `run_weekly_inspection() -> None`. This step stops before trade execution (Task 9 adds that) — ends by logging the parsed trade block and posting a "no material changes" notification if applicable.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_inspection_run.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — rh_client is fully mocked in every
# test below, and settings.rh_username/rh_password default to None (Optional),
# so no real value is ever needed. See Task 6's note on the alphabetical-
# collision regression with tests/test_config_rh.py found in Task 5.

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_position(symbol="NVDA", qty=10.0, avg_entry=400.0, current_price=450.0):
    return {
        "symbol": symbol, "qty": qty, "avg_entry_price": avg_entry,
        "current_price": current_price, "unrealized_pl": (current_price - avg_entry) * qty,
        "unrealized_plpc": (current_price / avg_entry - 1) * 100,
    }


@pytest.mark.asyncio
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection.rh_client")
async def test_skips_when_rh_session_unavailable(mock_rh, mock_notify):
    mock_rh.available = False
    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()
    mock_notify.assert_awaited_once()
    assert "offline" in mock_notify.call_args[0][0]["title"].lower() or \
           "offline" in mock_notify.call_args[0][0].get("description", "").lower()


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_no_changes_posts_notification_and_logs(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse, mock_notify, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[_mock_position()])
    mock_call.return_value = "analysis text ```json\n{}\n```"
    mock_parse.return_value = {"no_changes": True, "trades": []}

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_log.assert_called_once()
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "no_changes"
    assert any("no material changes" in c.kwargs.get("description", "").lower()
               or "no material changes" in str(c.args).lower()
               for c in mock_notify.call_args_list)


@pytest.mark.asyncio
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._call_claude_inspection_sync", side_effect=RuntimeError("API down"))
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_claude_api_failure_notifies_and_does_not_raise(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_notify,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[_mock_position()])

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()  # must not raise

    assert mock_notify.await_count >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claude_inspection_run.py -v`
Expected: FAIL — `run_weekly_inspection` currently raises `NotImplementedError` (the Task 3 placeholder), so every test in this file fails on that exception rather than on missing-attribute/import errors.

- [ ] **Step 3: Implement `run_weekly_inspection()` (data gathering through parse)**

**Replace the Task 3 placeholder function entirely** (the `async def run_weekly_inspection() -> None: raise NotImplementedError(...)` stub and its docstring) with the real implementation below. Also add these new imports near the top of `app/claude_inspection.py`, alongside the existing `import json`/`import logging`/`import os`:

```python
import asyncio
import json as _json
from datetime import datetime

import pytz

from app.claude_manager import (
    _embed, _timestamp, _fetch_yf_data, _fetch_technical_data,
    _CLR_ORANGE, _CLR_GREEN, _CLR_GRAY, _LOG_PATH,
)
from app.notifications import notify_claude_manager_embed, notify_claude_signal_feed
from app.trading.robinhood_client import rh_client

_CT = pytz.timezone("America/Chicago")


def _load_recent_rebalance_records(limit: int = 1) -> list:
    try:
        with open(_LOG_PATH) as f:
            records = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return []
    return records[-limit:]


async def run_weekly_inspection() -> None:
    """Weekly holdings-only review. Never opens a new position — see
    docs/superpowers/specs/2026-07-09-kimi-inspection-design.md."""
    log_entry: dict = {
        "timestamp": datetime.now(_CT).isoformat(),
        "status": "started",
        "holdings_reviewed": [],
        "trades_executed": [],
        "trades_skipped": [],
        "notes": {},
    }

    if not rh_client.available:
        log.warning("RH session unavailable — skipping weekly inspection")
        await notify_claude_manager_embed(_embed(
            "⚠️ INSPECTION SKIPPED — Robinhood session is offline",
            _CLR_ORANGE, footer=_timestamp(),
        ))
        return

    try:
        positions = await rh_client.get_all_positions_async()
        if not positions:
            log_entry["status"] = "no_holdings"
            await notify_claude_manager_embed(_embed(
                "🔍 KIMI INSPECTION — no current holdings to review",
                _CLR_GRAY, footer=_timestamp(),
            ))
            return

        loop = asyncio.get_running_loop()
        yf_tasks = [loop.run_in_executor(None, _fetch_yf_data, pos["symbol"]) for pos in positions]
        fv_tasks = [loop.run_in_executor(None, _fetch_technical_data, pos["symbol"]) for pos in positions]
        yf_results, fv_results = await asyncio.gather(
            asyncio.gather(*yf_tasks, return_exceptions=True),
            asyncio.gather(*fv_tasks, return_exceptions=True),
        )

        enriched = []
        for pos, yf_data, fv_data in zip(positions, yf_results, fv_results):
            yf_data = yf_data if isinstance(yf_data, dict) else {"ticker": pos["symbol"]}
            fv_data = fv_data if isinstance(fv_data, dict) else {}
            enriched.append({
                **yf_data, **fv_data,
                "qty": pos["qty"],
                "avg_entry_price": pos["avg_entry_price"],
                "current_price": pos.get("current_price"),
                "unrealized_pnl_pct": round(pos.get("unrealized_plpc", 0), 2),
            })
        log_entry["holdings_reviewed"] = [e["ticker"] for e in enriched if e.get("ticker")]

        rebalance_records = _load_recent_rebalance_records(limit=1)
        inspection_records = _load_recent_inspection_entries(limit=5)
        thesis_map = _build_prior_thesis_map(rebalance_records, inspection_records)

        holdings_json = _json.dumps(enriched, indent=2)
        thesis_lines = "\n\n".join(
            f"### {ticker}\n{thesis_map.get(ticker, 'No prior thesis on record — treat conservatively.')}"
            for ticker in log_entry["holdings_reviewed"]
        )
        prompt = (
            f"Weekly Inspection — review current holdings for anything material since the last check-in.\n\n"
            f"Current Holdings:\n{holdings_json}\n\n"
            f"Most Recent Thesis Per Ticker:\n{thesis_lines}\n\n"
            f"For each holding, decide HOLD / SELL / TRIM / DOUBLE_DOWN per the rules in your system prompt. "
            f"End with the required JSON block."
        )

        try:
            response_text = await loop.run_in_executor(None, _call_claude_inspection_sync, prompt)
        except Exception as exc:
            log.error("Inspection Claude API call failed: %s", exc)
            log_entry["status"] = "failed_claude_api"
            _append_inspection_log(log_entry)
            await notify_claude_manager_embed(_embed(
                "❌ INSPECTION FAILED — Anthropic API error",
                _CLR_ORANGE, description=str(exc), footer=_timestamp(),
            ))
            return

        trade_block = _parse_inspection_trade_block(response_text)

        if trade_block is None:
            log_entry["status"] = "failed_parse_or_buy_rejected"
            _append_inspection_log(log_entry)
            await notify_claude_manager_embed(_embed(
                "⚠️ INSPECTION — could not parse response (or a disallowed BUY was proposed)",
                _CLR_ORANGE,
                description="No trades executed this week — see logs for the raw response.",
                footer=_timestamp(),
            ))
            return

        if trade_block.get("no_changes") or not [
            t for t in trade_block.get("trades", []) if t.get("action") != "HOLD"
        ]:
            log_entry["status"] = "no_changes"
            _append_inspection_log(log_entry)
            await notify_claude_manager_embed(_embed(
                "🔍 KIMI INSPECTION — no material changes this week",
                _CLR_GREEN,
                description=f"Reviewed {len(log_entry['holdings_reviewed'])} holding(s); no action needed.",
                footer=_timestamp(),
            ))
            return

        # Task 9 continues here with trade execution for the non-HOLD trades.
        log_entry["_pending_trades"] = [t for t in trade_block["trades"] if t.get("action") != "HOLD"]
        log_entry["status"] = "trades_pending_execution"
        _append_inspection_log(log_entry)

    except Exception as exc:
        log.error("Unhandled error in run_weekly_inspection: %s", exc)
        await notify_claude_manager_embed(_embed(
            "❌ INSPECTION FAILED — unexpected error",
            _CLR_ORANGE, description=str(exc), footer=_timestamp(),
        ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claude_inspection_run.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/claude_inspection.py tests/test_claude_inspection_run.py
git commit -m "feat: run_weekly_inspection data gathering, delta-check prompt, and response parsing"
```

---

### Task 9: Trade execution (SELL/TRIM/DOUBLE_DOWN) + dual-channel Discord notifications

**Files:**
- Modify: `app/claude_inspection.py` (replace the Task 8 placeholder ending — the `log_entry["_pending_trades"]` block — with full execution)
- Test: Extend `tests/test_claude_inspection_run.py`

**Interfaces:**
- Consumes: `rh_client.close_ticker_async`/`sell_shares_async`/`buy_dollars_async` (`app.trading.robinhood_client`), `open_position`/`close_position`/`trim_position`/`get_record` (`app.claude_portfolio`), `_trade_embed`/`_field` (`app.claude_manager`), `notify_claude_signal_feed` (`app.notifications`, already imported in Task 8).
- Produces: complete `run_weekly_inspection()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_inspection_run.py`:

```python
@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection.get_record", return_value=(5, 2))
@patch("app.claude_inspection.close_position", return_value=(10.0, 500.0, 12.5))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NOW"})
@patch("app.claude_inspection.rh_client")
async def test_sell_action_executes_and_notifies_both_channels(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_close_position, mock_get_record, mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NOW", "qty": 5.0, "avg_entry_price": 900.0,
                       "current_price": 950.0, "unrealized_pl": 250.0, "unrealized_plpc": 5.5}]
    )
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "ok", "qty": 5.0, "fill_price": 960.0, "queued": False}
    )
    mock_call.return_value = "thesis broken ```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "SELL", "ticker": "NOW", "reasoning": "guidance cut"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_rh.close_ticker_async.assert_awaited_once_with("NOW")
    mock_close_position.assert_called_once()
    assert mock_notify_private.await_count >= 1   # Private Server gets full detail
    assert mock_notify_public.await_count >= 1    # KI Server gets the actioned-holding summary
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "completed"
    assert logged_entry["trades_executed"][0]["action"] == "SELL"


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_double_down_never_calls_open_position_for_a_buy_action(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    """Belt-and-suspenders: even if a BUY somehow reached this point, the
    execution loop below only dispatches SELL/TRIM/DOUBLE_DOWN branches —
    there is no BUY branch to accidentally execute."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 400.0,
                       "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5}]
    )
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {"no_changes": True, "trades": []}

    from app.claude_inspection import run_weekly_inspection
    import inspect
    source = inspect.getsource(run_weekly_inspection)
    assert '"BUY"' not in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claude_inspection_run.py -v`
Expected: FAIL — the Task 8 implementation stops at `_pending_trades` without executing anything, so `mock_rh.close_ticker_async` is never called and notification counts are wrong.

- [ ] **Step 3: Replace the Task 8 placeholder with full execution**

In `app/claude_inspection.py`, add these imports near the top of the file (alongside the Task 8 imports):

```python
from app.claude_manager import _trade_embed, _field, _CLR_RED
from app.claude_portfolio import open_position, close_position, trim_position, get_record
```

Replace the block from Task 8 that reads:

```python
        # Task 9 continues here with trade execution for the non-HOLD trades.
        log_entry["_pending_trades"] = [t for t in trade_block["trades"] if t.get("action") != "HOLD"]
        log_entry["status"] = "trades_pending_execution"
        _append_inspection_log(log_entry)
```

with:

```python
        pending_trades = [t for t in trade_block["trades"] if t.get("action") != "HOLD"]
        position_by_ticker = {p["symbol"]: p for p in positions}
        portfolio_value = sum(p["qty"] * p.get("current_price", 0) for p in positions)

        await notify_claude_manager_embed(_embed(
            f"🔍 KIMI INSPECTION — {len(pending_trades)} action(s) this week",
            _CLR_ORANGE, footer=_timestamp(),
        ))

        for trade in pending_trades:
            ticker = trade["ticker"].upper()
            action = trade["action"]
            reasoning = trade.get("reasoning", "")
            log_entry["notes"][ticker] = reasoning or f"{action} — see full analysis in Discord."
            pos = position_by_ticker.get(ticker)

            if pos is None:
                log_entry["trades_skipped"].append({"action": action, "ticker": ticker, "reason": "no position"})
                continue

            if action == "SELL":
                result = await rh_client.close_ticker_async(ticker)
                if result.get("status") != "ok" or not result.get("qty"):
                    reason = result.get("reason", result.get("note", "unknown"))
                    log_entry["trades_skipped"].append({"action": "SELL", "ticker": ticker, "reason": reason})
                    await asyncio.sleep(0.8)
                    await notify_claude_manager_embed(_embed(
                        f"❌ KIMI INSPECTION SELL — {ticker} FAILED",
                        _CLR_RED, description=reason, footer=_timestamp(),
                    ))
                    continue
                qty = result["qty"]
                fill = result.get("fill_price") or result.get("price_est")
                _, dollar_pnl, pct_pnl = close_position(ticker, fill or 0.0)
                wins, losses = get_record()
                log_entry["trades_executed"].append({
                    "action": "SELL", "ticker": ticker, "qty": qty,
                    "fill_price": fill, "dollar_pnl": dollar_pnl, "reasoning": reasoning,
                })
                pnl_str = (f"+${dollar_pnl:,.2f} ({pct_pnl:+.2f}%)" if dollar_pnl is not None else "—")
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_trade_embed(
                    "SELL", ticker,
                    [_field("Qty", f"{qty:g} shares @ ${fill or 0:,.2f}"),
                     _field("Record", f"{wins}W — {losses}L"),
                     _field("Reasoning", reasoning or "—", inline=False),
                     _field("P&L", pnl_str, inline=False)],
                    _timestamp(),
                ))
                await notify_claude_signal_feed(
                    f"🔴 **KIMI INSPECTION SELL — {ticker}**\n{reasoning or 'See analysis.'}\n"
                    f"@ ${fill or 0:,.2f}\n{_timestamp()}"
                )

            elif action == "TRIM":
                target_wt = trade.get("target_weight_pct", 5)
                target_value = portfolio_value * target_wt / 100
                current_qty = pos["qty"]
                current_price = pos.get("current_price", 0)
                current_value = current_qty * current_price
                if current_qty < 1.0 or target_value >= current_value * 0.95:
                    reason = "fractional position" if current_qty < 1.0 else "already at target"
                    log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": reason})
                    continue
                sell_qty = round((current_value - target_value) / current_price, 6) if current_price > 0 else 0.0
                if sell_qty <= 0:
                    log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": "sell qty <= 0"})
                    continue
                result = await rh_client.sell_shares_async(ticker, sell_qty)
                if result.get("status") != "ok":
                    reason = result.get("reason", "unknown")
                    log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": reason})
                    await asyncio.sleep(0.8)
                    await notify_claude_manager_embed(_embed(
                        f"❌ KIMI INSPECTION TRIM — {ticker} FAILED",
                        _CLR_RED, description=reason, footer=_timestamp(),
                    ))
                    continue
                qty_sold = result.get("qty", sell_qty)
                fill = result.get("fill_price") or result.get("price_est")
                _, dollar_pnl, pct_pnl = trim_position(ticker, qty_sold, fill or 0.0)
                wins, losses = get_record()
                log_entry["trades_executed"].append({
                    "action": "TRIM", "ticker": ticker, "qty": qty_sold,
                    "fill_price": fill, "dollar_pnl": dollar_pnl,
                    "target_weight_pct": target_wt, "reasoning": reasoning,
                })
                pnl_str = (f"+${dollar_pnl:,.2f} ({pct_pnl:+.2f}%)" if dollar_pnl is not None else "—")
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_trade_embed(
                    "TRIM", ticker,
                    [_field("Sold", f"{qty_sold:g} shares @ ${fill or 0:,.2f}"),
                     _field("→ Target", f"{target_wt}%"),
                     _field("Reasoning", reasoning or "—", inline=False),
                     _field("P&L", pnl_str, inline=False)],
                    _timestamp(),
                ))
                await notify_claude_signal_feed(
                    f"✂️ **KIMI INSPECTION TRIM — {ticker}**\n{reasoning or 'See analysis.'}\n"
                    f"→ target {target_wt}% · {_timestamp()}"
                )

            elif action == "DOUBLE_DOWN":
                target_wt = trade.get("target_weight_pct", 10)
                target_dollars = portfolio_value * target_wt / 100
                current_val = pos["qty"] * pos.get("current_price", 0)
                buying_power = await rh_client.get_buying_power_async() or 0.0
                delta_dollars = max(0.0, target_dollars - current_val)
                invest_dollars = min(delta_dollars, buying_power * 0.95)
                if invest_dollars < 1:
                    log_entry["trades_skipped"].append({
                        "action": "DOUBLE_DOWN", "ticker": ticker,
                        "reason": f"needed ${delta_dollars:,.0f}, only ${buying_power:,.0f} available",
                    })
                    continue
                result = await rh_client.buy_dollars_async(ticker, invest_dollars)
                if result.get("status") != "ok":
                    reason = result.get("reason", "unknown")
                    log_entry["trades_skipped"].append({"action": "DOUBLE_DOWN", "ticker": ticker, "reason": reason})
                    await asyncio.sleep(0.8)
                    await notify_claude_manager_embed(_embed(
                        f"❌ KIMI INSPECTION DOUBLE_DOWN — {ticker} FAILED",
                        _CLR_RED, description=reason, footer=_timestamp(),
                    ))
                    continue
                qty = result.get("qty", 0)
                fill = result.get("fill_price") or result.get("price_est", 0)
                open_position(ticker, qty, fill or 0.0)
                log_entry["trades_executed"].append({
                    "action": "DOUBLE_DOWN", "ticker": ticker, "qty": qty,
                    "fill_price": fill, "dollars_invested": invest_dollars,
                    "target_weight_pct": target_wt, "reasoning": reasoning,
                })
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_trade_embed(
                    "DOUBLE_DOWN", ticker,
                    [_field("Qty", f"{qty:g} shares @ ${fill or 0:,.2f}"),
                     _field("Target Weight", f"{target_wt}%"),
                     _field("Reasoning", reasoning or "—", inline=False)],
                    _timestamp(),
                ))
                await notify_claude_signal_feed(
                    f"🔥 **KIMI INSPECTION DOUBLE_DOWN — {ticker}**\n{reasoning or 'See analysis.'}\n"
                    f"Target: {target_wt}% · {_timestamp()}"
                )

        log_entry["status"] = "completed"
        _append_inspection_log(log_entry)

        executed = len(log_entry["trades_executed"])
        skipped = len(log_entry["trades_skipped"])
        await asyncio.sleep(0.8)
        await notify_claude_manager_embed(_embed(
            "✅ KIMI INSPECTION COMPLETE",
            _CLR_GREEN,
            description=f"{executed} trade(s) executed"
                        + (f", {skipped} skipped" if skipped else ""),
            footer=_timestamp(),
        ))
```

(This is a second, separate `from app.claude_manager import ...` line — leave the Task 8 import line untouched; Python allows multiple import statements from the same module in one file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claude_inspection_run.py -v`
Expected: 5 passed (3 from Task 8 plus the 2 new ones)

- [ ] **Step 5: Run the full test suite for regressions**

Run: `pytest -v`
Expected: all pass, including every previously-existing test file

- [ ] **Step 6: Commit**

```bash
git add app/claude_inspection.py tests/test_claude_inspection_run.py
git commit -m "feat: execute Inspection SELL/TRIM/DOUBLE_DOWN trades with dual-channel Discord notifications"
```

---

### Task 10: Extend `_load_recent_history()` with recent Inspection activity

**Files:**
- Modify: `app/claude_manager.py:530-568` (`_load_recent_history`)
- Test: Create `tests/test_claude_manager_history.py`

**Interfaces:**
- Consumes: `_load_recent_inspection_entries()` from `app.claude_inspection` (Task 3) — lazy import inside the function to avoid a circular import (`claude_inspection` already imports from `claude_manager`).
- Produces: `_load_recent_history()` — same signature `() -> tuple[list, str]`, unchanged return type; only the formatted string gains a new section. The raw `records` list returned is unchanged (still rebalance-only) so `_format_benchmark`'s month-over-month comparison logic isn't affected by mixing in weekly entries.

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_manager_history.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — see Task 6's note on the
# test_config_rh.py alphabetical-collision regression found in Task 5.

import json
from unittest.mock import patch


def test_history_string_includes_recent_inspection_activity(tmp_path):
    rebalance_log = tmp_path / "claude_rebalance_log.json"
    rebalance_log.write_text(json.dumps([{
        "timestamp": "2026-07-01T09:35:00", "status": "completed",
        "portfolio_value": 10000.0, "trades_executed": [], "analysis_body": "",
    }]))

    inspection_entries = [{
        "timestamp": "2026-07-13T09:35:00", "status": "completed",
        "trades_executed": [{"action": "SELL", "ticker": "NOW"}],
        "notes": {"NOW": "Guidance cut on Jul 10 earnings call — closed position."},
    }]

    with patch("app.claude_manager._LOG_PATH", str(rebalance_log)), \
         patch("app.claude_inspection._load_recent_inspection_entries", return_value=inspection_entries):
        from app.claude_manager import _load_recent_history
        _, history_str = _load_recent_history()

    assert "Guidance cut on Jul 10" in history_str
    assert "SELL" in history_str and "NOW" in history_str


def test_history_string_unchanged_when_no_inspection_activity(tmp_path):
    rebalance_log = tmp_path / "claude_rebalance_log.json"
    rebalance_log.write_text(json.dumps([{
        "timestamp": "2026-07-01T09:35:00", "status": "completed",
        "portfolio_value": 10000.0, "trades_executed": [], "analysis_body": "",
    }]))

    with patch("app.claude_manager._LOG_PATH", str(rebalance_log)), \
         patch("app.claude_inspection._load_recent_inspection_entries", return_value=[]):
        from app.claude_manager import _load_recent_history
        records, history_str = _load_recent_history()

    assert len(records) == 1
    assert "Inspection" not in history_str
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_claude_manager_history.py -v`
Expected: FAIL — `_load_recent_history` doesn't yet read from `claude_inspection` at all, so the first test's assertions on inspection content fail.

- [ ] **Step 3: Extend `_load_recent_history()`**

In `app/claude_manager.py`, replace the `return records, "\n".join(lines)` line at the end of `_load_recent_history()` (line 568) — insert the new section immediately before that final return:

```python
    # Weave in any weekly Inspection activity since the last rebalance, so the
    # monthly rebalance can build on what Inspection already decided instead
    # of re-deriving a thesis Inspection already updated.
    from app.claude_inspection import _load_recent_inspection_entries
    last_rebalance_ts = recent[-1].get("timestamp", "") if recent else ""
    inspection_entries = [
        e for e in _load_recent_inspection_entries(limit=10)
        if e.get("timestamp", "") > last_rebalance_ts
        and any(t for t in e.get("trades_executed", []))
    ]
    if inspection_entries:
        lines.append("\n--- Weekly Inspection Activity Since Last Rebalance ---")
        for entry in inspection_entries:
            ts = entry.get("timestamp", "unknown")[:10]
            for trade in entry.get("trades_executed", []):
                ticker = trade.get("ticker", "?")
                action = trade.get("action", "?")
                note = entry.get("notes", {}).get(ticker, "")
                lines.append(f"  {ts}: {action} {ticker} — {note}")
        lines.append("--- End Inspection Activity ---")

    return records, "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_claude_manager_history.py -v`
Expected: 2 passed

- [ ] **Step 5: Run the full test suite for regressions**

Run: `pytest -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add app/claude_manager.py tests/test_claude_manager_history.py
git commit -m "feat: monthly rebalance history now includes recent weekly Inspection activity"
```

---

## Post-implementation checklist (not automated — do before relying on this in production)

- [ ] Confirm `CLAUDE_INSPECTION_LOG_PATH` (or its default `/data/claude_inspection_log.json`) is on the same persistent Render disk as `claude_rebalance_log.json`, so it survives restarts like the other `/data/*.json` files.
- [ ] Manually trigger `run_weekly_inspection()` once against the real (paper or live) RH session before the first scheduled fire, the same way `/rebalance` lets you dry-run the monthly job on demand — consider adding a matching `/inspect` Discord slash command or `POST /run-inspection` endpoint if you want that same manual-trigger capability (out of scope for this plan; call it out separately if wanted).
- [ ] Watch the first 2-3 live weekly runs closely given this spec noted "same quality" is a hypothesis to validate, not a guarantee — this applies here too, just for the lighter-weight feature rather than the rejected Board design.
