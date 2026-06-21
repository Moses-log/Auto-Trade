# Delayed Withdrawal Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/withdraw` (both the Discord command and the `POST /withdraw` HTTP endpoint) schedule a delayed, cancelable withdrawal instead of writing to `investors.json` immediately, so a compromised credential can't drain investor funds without the real operator getting a window to notice and stop it.

**Architecture:** A new storage layer (`pending_withdrawals.json`, `withdrawal_audit.json`) mirrors the existing `pending_orders.json` pattern. A single business-logic module (`app/withdrawal_execution.py`) exposes `schedule_withdrawal()`, `execute_pending_withdrawal()`, and `cancel_pending_withdrawal()` — both the Discord command and the HTTP endpoint call the same `schedule_withdrawal()` so there is exactly one place validation happens. APScheduler (already used for `pending_orders.json`) fires `execute_pending_withdrawal()` at `run_at`; a startup rescheduling hook (mirroring `reschedule_pending_orders()`) makes pending withdrawals survive app restarts.

**Tech Stack:** Python 3, FastAPI, APScheduler (`AsyncIOScheduler`), pytest + pytest-asyncio, existing `app.investors` FIFO withdrawal logic (unchanged).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-21-withdrawal-approval-design.md`
- Default delay is `24` hours, configurable via `WITHDRAWAL_DELAY_HOURS` env var (`app/config.py`, new `withdrawal_delay_hours: int = 24` setting).
- `/deposit`, `/close`, `/rebalance`, `/rh_deposit` are NOT touched by this plan.
- No second notification channel — alerts stay Discord-only (`notify_investors()` / `_edit_original()`), per the approved spec.
- `discord_your_user_id` single-value auth is unchanged — this plan adds friction to the one existing authorized user, not a second approver.
- Every pending withdrawal's terminal outcome (executed, canceled, or failed) is recorded in `withdrawal_audit.json` — one append-only log for all three outcomes, not separate files.
- Both `POST /withdraw` (HTTP, `main.py:776`) and the Discord `/withdraw` command MUST go through the same `schedule_withdrawal()` function — this is the fix for the bypass identified during planning (see spec, "Scope includes a second entry point").
- JSON storage files use the existing atomic-write pattern: write to a `.tmp` sibling, then `Path.replace()` (see `app/pending_orders.py:25-28`).

---

### Task 1: Pending-withdrawal and audit storage modules

**Files:**
- Create: `app/pending_withdrawals.py`
- Create: `app/withdrawal_audit.py`
- Test: `tests/test_pending_withdrawals.py`
- Test: `tests/test_withdrawal_audit.py`

**Interfaces:**
- Produces (used by Task 3 and Task 4):
  - `save_pending_withdrawal(withdrawal_id: str, investor: str, amount: float, requested_at: str, run_at: str) -> None`
  - `remove_pending_withdrawal(withdrawal_id: str) -> None`
  - `load_pending_withdrawals() -> list[dict]`
  - `get_pending_withdrawal(withdrawal_id: str) -> Optional[dict]`
  - `append_withdrawal_audit(withdrawal_id: str, investor: str, amount: float, requested_at: str, run_at: str, status: str, **extra) -> None`
  - `load_withdrawal_audit() -> list[dict]`

This task has no dependency on any other task — it's a pure JSON-storage layer, directly mirroring `app/pending_orders.py`.

- [ ] **Step 1: Write the failing tests for `pending_withdrawals.py`**

Create `tests/test_pending_withdrawals.py`:

```python
import json
import pytest

import app.pending_withdrawals as pw


@pytest.fixture(autouse=True)
def _isolate_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "_FILE", tmp_path / "pending_withdrawals.json")


def test_load_returns_empty_list_when_file_missing():
    assert pw.load_pending_withdrawals() == []


def test_save_then_load_roundtrip():
    pw.save_pending_withdrawal(
        withdrawal_id="wd-aaaa1111",
        investor="Moses",
        amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00",
        run_at="2026-06-22T10:00:00-05:00",
    )
    pending = pw.load_pending_withdrawals()
    assert len(pending) == 1
    assert pending[0]["id"] == "wd-aaaa1111"
    assert pending[0]["investor"] == "Moses"
    assert pending[0]["amount"] == 500.0
    assert pending[0]["run_at"] == "2026-06-22T10:00:00-05:00"


def test_save_appends_multiple_records():
    pw.save_pending_withdrawal(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
    )
    pw.save_pending_withdrawal(
        withdrawal_id="wd-bbbb2222", investor="Gabe", amount=200.0,
        requested_at="2026-06-21T11:00:00-05:00", run_at="2026-06-22T11:00:00-05:00",
    )
    pending = pw.load_pending_withdrawals()
    assert {p["id"] for p in pending} == {"wd-aaaa1111", "wd-bbbb2222"}


def test_remove_pending_withdrawal_removes_only_matching_id():
    pw.save_pending_withdrawal(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
    )
    pw.save_pending_withdrawal(
        withdrawal_id="wd-bbbb2222", investor="Gabe", amount=200.0,
        requested_at="2026-06-21T11:00:00-05:00", run_at="2026-06-22T11:00:00-05:00",
    )
    pw.remove_pending_withdrawal("wd-aaaa1111")
    pending = pw.load_pending_withdrawals()
    assert len(pending) == 1
    assert pending[0]["id"] == "wd-bbbb2222"


def test_get_pending_withdrawal_returns_none_when_not_found():
    assert pw.get_pending_withdrawal("wd-missing") is None


def test_get_pending_withdrawal_returns_matching_record():
    pw.save_pending_withdrawal(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
    )
    record = pw.get_pending_withdrawal("wd-aaaa1111")
    assert record["investor"] == "Moses"


def test_load_returns_empty_list_on_corrupt_json(tmp_path, monkeypatch):
    bad_file = tmp_path / "corrupt.json"
    bad_file.write_text("not valid json")
    monkeypatch.setattr(pw, "_FILE", bad_file)
    assert pw.load_pending_withdrawals() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_pending_withdrawals.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pending_withdrawals'`

- [ ] **Step 3: Implement `app/pending_withdrawals.py`**

```python
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_FILE = Path(os.getenv("PENDING_WITHDRAWALS_PATH", "/data/pending_withdrawals.json"))
_lock = threading.Lock()


def _load() -> list:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text()).get("pending", [])
        except Exception:
            return []
    return []


def _save(records: list) -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pending": records}))
    tmp.replace(_FILE)  # atomic rename — survives kill signals mid-write


def save_pending_withdrawal(
    withdrawal_id: str,
    investor: str,
    amount: float,
    requested_at: str,
    run_at: str,
) -> None:
    with _lock:
        records = _load()
        records.append({
            "id": withdrawal_id,
            "investor": investor,
            "amount": amount,
            "requested_at": requested_at,
            "run_at": run_at,
        })
        _save(records)
    log.info("Saved pending withdrawal %s for %s ($%.2f)", withdrawal_id, investor, amount)


def remove_pending_withdrawal(withdrawal_id: str) -> None:
    with _lock:
        records = _load()
        records = [r for r in records if r["id"] != withdrawal_id]
        _save(records)
    log.info("Removed pending withdrawal %s from disk", withdrawal_id)


def load_pending_withdrawals() -> list:
    return _load()


def get_pending_withdrawal(withdrawal_id: str) -> Optional[dict]:
    for record in _load():
        if record["id"] == withdrawal_id:
            return record
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_pending_withdrawals.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Write the failing tests for `withdrawal_audit.py`**

Create `tests/test_withdrawal_audit.py`:

```python
import pytest

import app.withdrawal_audit as wa


@pytest.fixture(autouse=True)
def _isolate_file(tmp_path, monkeypatch):
    monkeypatch.setattr(wa, "_FILE", tmp_path / "withdrawal_audit.json")


def test_load_returns_empty_list_when_file_missing():
    assert wa.load_withdrawal_audit() == []


def test_append_executed_record():
    wa.append_withdrawal_audit(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
        status="executed",
    )
    audit = wa.load_withdrawal_audit()
    assert len(audit) == 1
    assert audit[0]["status"] == "executed"
    assert audit[0]["id"] == "wd-aaaa1111"


def test_append_failed_record_with_reason():
    wa.append_withdrawal_audit(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
        status="failed", reason="Withdrawal exceeds available equity",
    )
    audit = wa.load_withdrawal_audit()
    assert audit[0]["status"] == "failed"
    assert audit[0]["reason"] == "Withdrawal exceeds available equity"


def test_append_canceled_record_with_timestamp():
    wa.append_withdrawal_audit(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
        status="canceled", canceled_at="2026-06-21T12:00:00-05:00",
    )
    audit = wa.load_withdrawal_audit()
    assert audit[0]["status"] == "canceled"
    assert audit[0]["canceled_at"] == "2026-06-21T12:00:00-05:00"


def test_audit_log_is_append_only_across_multiple_entries():
    wa.append_withdrawal_audit(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="t1", run_at="t2", status="canceled",
    )
    wa.append_withdrawal_audit(
        withdrawal_id="wd-bbbb2222", investor="Gabe", amount=200.0,
        requested_at="t3", run_at="t4", status="executed",
    )
    audit = wa.load_withdrawal_audit()
    assert len(audit) == 2
    assert [a["id"] for a in audit] == ["wd-aaaa1111", "wd-bbbb2222"]
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `python -m pytest tests/test_withdrawal_audit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.withdrawal_audit'`

- [ ] **Step 7: Implement `app/withdrawal_audit.py`**

```python
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_FILE = Path(os.getenv("WITHDRAWAL_AUDIT_PATH", "/data/withdrawal_audit.json"))
_lock = threading.Lock()


def _load() -> list:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text()).get("audit", [])
        except Exception:
            return []
    return []


def _save(records: list) -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"audit": records}))
    tmp.replace(_FILE)


def append_withdrawal_audit(
    withdrawal_id: str,
    investor: str,
    amount: float,
    requested_at: str,
    run_at: str,
    status: str,
    **extra,
) -> None:
    with _lock:
        records = _load()
        entry = {
            "id": withdrawal_id,
            "investor": investor,
            "amount": amount,
            "requested_at": requested_at,
            "run_at": run_at,
            "status": status,
        }
        entry.update(extra)
        records.append(entry)
        _save(records)
    log.info("Recorded withdrawal audit entry %s: %s", withdrawal_id, status)


def load_withdrawal_audit() -> list:
    return _load()
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `python -m pytest tests/test_withdrawal_audit.py -v`
Expected: PASS (5 tests)

- [ ] **Step 9: Commit**

```bash
git add app/pending_withdrawals.py app/withdrawal_audit.py tests/test_pending_withdrawals.py tests/test_withdrawal_audit.py
git commit -m "feat: add pending-withdrawal and withdrawal-audit storage modules"
```

---

### Task 2: Withdrawal execution module — schedule, execute, cancel

**Files:**
- Create: `app/withdrawal_execution.py`
- Modify: `app/config.py` (add `withdrawal_delay_hours` setting)
- Modify: `.env.example` (document `WITHDRAWAL_DELAY_HOURS`)
- Test: `tests/test_withdrawal_execution.py`

**Interfaces:**
- Consumes (from Task 1): `save_pending_withdrawal`, `remove_pending_withdrawal`, `get_pending_withdrawal` from `app.pending_withdrawals`; `append_withdrawal_audit` from `app.withdrawal_audit`.
- Consumes (existing code): `compute_withdrawal_lots`, `format_withdrawal_message`, `load_investors`, `save_investors`, `investors_lock`, `Withdrawal` from `app.investors`; `get_latest_price` from `app.trading.alpaca_client`; `scheduler` from `app.scheduler`; `settings` from `app.config`.
- Produces (used by Task 4, Task 5, Task 6, Task 7):
  - `class WithdrawalValidationError(Exception)`
  - `class WithdrawalNotFoundError(Exception)`
  - `async def schedule_withdrawal(investor_name: str, amount: float, spy_price: Optional[float] = None) -> dict` — returns `{"id", "investor", "amount", "requested_at", "run_at"}`; raises `WithdrawalValidationError` with a user-facing message on failure. `spy_price`, if given, is used only for the request-time validation check (the eventual execution always re-fetches a live price).
  - `async def execute_pending_withdrawal(withdrawal_id: str) -> None` — scheduler job target, no return value.
  - `async def cancel_pending_withdrawal(withdrawal_id: str) -> dict` — returns the canceled pending record; raises `WithdrawalNotFoundError` if no such pending withdrawal exists.

- [ ] **Step 1: Add the `withdrawal_delay_hours` setting**

In `app/config.py`, add after the `discord_your_user_id` line (currently line 44):

```python
    discord_your_user_id: Optional[str] = None

    # ── Withdrawal approval delay ─────────────────────────────────────────────
    # Hours a /withdraw request waits before it actually executes. Gives the
    # operator a window to /cancel-withdrawal a request made by a compromised
    # credential before investor funds are actually moved in the ledger.
    withdrawal_delay_hours: int = 24
```

In `.env.example`, add after the `DISCORD_YOUR_USER_ID=` line:

```
# Hours a withdrawal waits before executing (gives you time to /cancel-withdrawal
# a request you didn't make). Default 24.
WITHDRAWAL_DELAY_HOURS=24
```

- [ ] **Step 2: Write the failing tests for `schedule_withdrawal`**

Create `tests/test_withdrawal_execution.py`:

```python
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

import pytz

from app.investors import Deposit, Investor

_CT = pytz.timezone("America/Chicago")


def _moses(deposits_amount=2000.0, entry_spy=707.0):
    return Investor(name="Moses", deposits=[
        Deposit(amount=deposits_amount, entry_spy=entry_spy, date="2026-05-09")
    ])


@pytest.mark.asyncio
async def test_schedule_withdrawal_rejects_non_positive_amount():
    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError
    with pytest.raises(WithdrawalValidationError, match="positive"):
        await schedule_withdrawal("Moses", 0.0)


@pytest.mark.asyncio
async def test_schedule_withdrawal_rejects_unknown_investor():
    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError
    with patch("app.withdrawal_execution.load_investors", return_value=[]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20):
        with pytest.raises(WithdrawalValidationError, match="not found"):
            await schedule_withdrawal("Ghost", 500.0)


@pytest.mark.asyncio
async def test_schedule_withdrawal_rejects_amount_exceeding_equity():
    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError
    inv = _moses(deposits_amount=300.0)
    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20):
        with pytest.raises(WithdrawalValidationError, match="exceeds"):
            await schedule_withdrawal("Moses", 5000.0)


@pytest.mark.asyncio
async def test_schedule_withdrawal_saves_pending_and_adds_scheduler_job():
    from app.withdrawal_execution import schedule_withdrawal
    inv = _moses()
    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.save_pending_withdrawal") as mock_save, \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler:
        record = await schedule_withdrawal("moses", 500.0)

    assert record["investor"] == "Moses"  # canonical case from the stored Investor, not user input
    assert record["amount"] == 500.0
    assert record["id"].startswith("wd-")
    mock_save.assert_called_once()
    mock_scheduler.add_job.assert_called_once()
    _, kwargs = mock_scheduler.add_job.call_args
    assert kwargs["id"] == f"withdrawal_{record['id']}"
    assert kwargs["args"] == [record["id"]]


@pytest.mark.asyncio
async def test_schedule_withdrawal_run_at_respects_delay_setting():
    from app.withdrawal_execution import schedule_withdrawal
    inv = _moses()
    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.save_pending_withdrawal"), \
         patch("app.withdrawal_execution.scheduler"), \
         patch("app.withdrawal_execution.settings") as mock_settings:
        mock_settings.withdrawal_delay_hours = 24
        before = datetime.now(_CT)
        record = await schedule_withdrawal("Moses", 500.0)
        run_at = datetime.fromisoformat(record["run_at"])

    assert run_at - before >= timedelta(hours=23, minutes=59)
    assert run_at - before <= timedelta(hours=24, minutes=1)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_withdrawal_execution.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.withdrawal_execution'`

- [ ] **Step 4: Implement `schedule_withdrawal` in `app/withdrawal_execution.py`**

```python
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

from app.config import settings
from app.investors import (
    Withdrawal,
    compute_withdrawal_lots,
    format_withdrawal_message,
    load_investors,
    save_investors,
    investors_lock,
)
from app.pending_withdrawals import (
    get_pending_withdrawal,
    remove_pending_withdrawal,
    save_pending_withdrawal,
)
from app.scheduler import scheduler
from app.trading.alpaca_client import get_latest_price
from app.withdrawal_audit import append_withdrawal_audit

log = logging.getLogger(__name__)

_CT = pytz.timezone("America/Chicago")


class WithdrawalValidationError(Exception):
    """Raised by schedule_withdrawal when the request can't be scheduled."""


class WithdrawalNotFoundError(Exception):
    """Raised by cancel_pending_withdrawal when the id doesn't match a pending withdrawal."""


async def schedule_withdrawal(
    investor_name: str,
    amount: float,
    spy_price: Optional[float] = None,
) -> dict:
    if amount <= 0:
        raise WithdrawalValidationError("Withdrawal amount must be positive")

    if spy_price is None:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            raise WithdrawalValidationError("Could not fetch SPY price — try again")

    investors = load_investors()
    inv = next((i for i in investors if i.name.lower() == investor_name.lower()), None)
    if inv is None:
        raise WithdrawalValidationError(f'Investor "{investor_name}" not found — check spelling')

    try:
        # Validation only — the result is discarded. Execution re-runs this with
        # a live price and the investor's state at execution time, not now.
        compute_withdrawal_lots(inv, amount, spy_price)
    except ValueError as exc:
        raise WithdrawalValidationError(str(exc)) from exc

    now = datetime.now(_CT)
    run_at = now + timedelta(hours=settings.withdrawal_delay_hours)
    withdrawal_id = f"wd-{uuid.uuid4().hex[:8]}"

    save_pending_withdrawal(
        withdrawal_id=withdrawal_id,
        investor=inv.name,
        amount=amount,
        requested_at=now.isoformat(),
        run_at=run_at.isoformat(),
    )

    scheduler.add_job(
        execute_pending_withdrawal,
        "date",
        run_date=run_at,
        args=[withdrawal_id],
        id=f"withdrawal_{withdrawal_id}",
        replace_existing=True,
    )

    log.info("Scheduled withdrawal %s for %s ($%.2f) at %s", withdrawal_id, inv.name, amount, run_at)
    return {
        "id": withdrawal_id,
        "investor": inv.name,
        "amount": amount,
        "requested_at": now.isoformat(),
        "run_at": run_at.isoformat(),
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_withdrawal_execution.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Write the failing tests for `execute_pending_withdrawal`**

Append to `tests/test_withdrawal_execution.py`:

```python
@pytest.mark.asyncio
async def test_execute_pending_withdrawal_writes_to_investors_and_audits_executed():
    from app.withdrawal_execution import execute_pending_withdrawal
    inv = _moses()
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.save_investors") as mock_save, \
         patch("app.withdrawal_execution.remove_pending_withdrawal") as mock_remove, \
         patch("app.withdrawal_execution.append_withdrawal_audit") as mock_audit, \
         patch("app.withdrawal_execution.notify_investors") as mock_notify, \
         patch("app.withdrawal_execution.push_backup") as mock_backup:
        mock_notify.return_value = _async_none()
        mock_backup.return_value = _async_none()
        await execute_pending_withdrawal("wd-aaaa1111")

    mock_save.assert_called_once()
    assert len(inv.withdrawals) == 1
    assert inv.withdrawals[0].proceeds == 500.0
    mock_remove.assert_called_once_with("wd-aaaa1111")
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["status"] == "executed"


@pytest.mark.asyncio
async def test_execute_pending_withdrawal_returns_silently_when_record_missing():
    from app.withdrawal_execution import execute_pending_withdrawal
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=None), \
         patch("app.withdrawal_execution.save_investors") as mock_save:
        await execute_pending_withdrawal("wd-gone")
    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_execute_pending_withdrawal_audits_failed_when_equity_insufficient():
    from app.withdrawal_execution import execute_pending_withdrawal
    inv = _moses(deposits_amount=100.0)  # not enough left for a $500 withdrawal
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.save_investors") as mock_save, \
         patch("app.withdrawal_execution.remove_pending_withdrawal") as mock_remove, \
         patch("app.withdrawal_execution.append_withdrawal_audit") as mock_audit, \
         patch("app.withdrawal_execution.notify_investors") as mock_notify:
        mock_notify.return_value = _async_none()
        await execute_pending_withdrawal("wd-aaaa1111")

    mock_save.assert_not_called()
    mock_remove.assert_called_once_with("wd-aaaa1111")
    assert mock_audit.call_args.kwargs["status"] == "failed"
    assert "reason" in mock_audit.call_args.kwargs


def _async_none():
    async def _coro():
        return None
    return _coro()
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `python -m pytest tests/test_withdrawal_execution.py -v -k execute_pending_withdrawal`
Expected: FAIL with `ImportError: cannot import name 'execute_pending_withdrawal'`

- [ ] **Step 8: Implement `execute_pending_withdrawal` and `cancel_pending_withdrawal`**

Append to `app/withdrawal_execution.py` (and add the two new imports at the top alongside the existing ones — `from app.notifications import notify_investors` and `from app.backup import push_backup` — plus `from apscheduler.jobstores.base import JobLookupError`):

```python
async def execute_pending_withdrawal(withdrawal_id: str) -> None:
    record = get_pending_withdrawal(withdrawal_id)
    if record is None:
        log.warning("execute_pending_withdrawal: %s not found (already canceled?)", withdrawal_id)
        return

    spy_price = get_latest_price("SPY")
    if spy_price is None:
        log.error("execute_pending_withdrawal: could not fetch SPY price for %s — leaving pending", withdrawal_id)
        return

    discord_msg = None
    error_reason = None

    async with investors_lock:
        investors = load_investors()
        inv = next((i for i in investors if i.name.lower() == record["investor"].lower()), None)
        if inv is None:
            error_reason = f'Investor "{record["investor"]}" no longer exists'
        else:
            try:
                lots, units_redeemed = compute_withdrawal_lots(inv, record["amount"], spy_price)
            except ValueError as exc:
                error_reason = str(exc)
            else:
                total_cost_basis = sum(lot["cost"] for lot in lots)
                discord_msg = format_withdrawal_message(inv, lots, units_redeemed, spy_price, record["amount"])
                inv.withdrawals.append(
                    Withdrawal(
                        units=units_redeemed,
                        exit_spy=spy_price,
                        cost_basis=total_cost_basis,
                        proceeds=record["amount"],
                        date=date.today().isoformat(),
                    )
                )
                save_investors(investors)

    remove_pending_withdrawal(withdrawal_id)

    if error_reason:
        append_withdrawal_audit(
            withdrawal_id=record["id"], investor=record["investor"], amount=record["amount"],
            requested_at=record["requested_at"], run_at=record["run_at"],
            status="failed", reason=error_reason,
        )
        await notify_investors(
            f"❌ Scheduled withdrawal for {record['investor']} (${record['amount']:,.2f}) failed: {error_reason}"
        )
        return

    append_withdrawal_audit(
        withdrawal_id=record["id"], investor=record["investor"], amount=record["amount"],
        requested_at=record["requested_at"], run_at=record["run_at"],
        status="executed", completed_at=datetime.now(_CT).isoformat(),
    )
    await notify_investors(discord_msg)
    await push_backup()


async def cancel_pending_withdrawal(withdrawal_id: str) -> dict:
    record = get_pending_withdrawal(withdrawal_id)
    if record is None:
        raise WithdrawalNotFoundError(
            f"No pending withdrawal with id {withdrawal_id} (already executed, canceled, or never existed)"
        )

    try:
        scheduler.remove_job(f"withdrawal_{withdrawal_id}")
    except JobLookupError:
        pass  # job already fired or was already removed; still clean up the record below

    remove_pending_withdrawal(withdrawal_id)
    append_withdrawal_audit(
        withdrawal_id=record["id"], investor=record["investor"], amount=record["amount"],
        requested_at=record["requested_at"], run_at=record["run_at"],
        status="canceled", canceled_at=datetime.now(_CT).isoformat(),
    )
    log.info("Canceled pending withdrawal %s for %s", withdrawal_id, record["investor"])
    return record
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `python -m pytest tests/test_withdrawal_execution.py -v -k execute_pending_withdrawal`
Expected: PASS (3 tests)

- [ ] **Step 10: Write the failing tests for `cancel_pending_withdrawal`**

Append to `tests/test_withdrawal_execution.py`:

```python
@pytest.mark.asyncio
async def test_cancel_pending_withdrawal_removes_job_and_record_and_audits():
    from app.withdrawal_execution import cancel_pending_withdrawal
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler, \
         patch("app.withdrawal_execution.remove_pending_withdrawal") as mock_remove, \
         patch("app.withdrawal_execution.append_withdrawal_audit") as mock_audit:
        result = await cancel_pending_withdrawal("wd-aaaa1111")

    mock_scheduler.remove_job.assert_called_once_with("withdrawal_wd-aaaa1111")
    mock_remove.assert_called_once_with("wd-aaaa1111")
    assert mock_audit.call_args.kwargs["status"] == "canceled"
    assert result["investor"] == "Moses"


@pytest.mark.asyncio
async def test_cancel_pending_withdrawal_raises_when_not_found():
    from app.withdrawal_execution import cancel_pending_withdrawal, WithdrawalNotFoundError
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=None):
        with pytest.raises(WithdrawalNotFoundError):
            await cancel_pending_withdrawal("wd-missing")


@pytest.mark.asyncio
async def test_cancel_pending_withdrawal_succeeds_even_if_job_already_gone():
    from app.withdrawal_execution import cancel_pending_withdrawal
    from apscheduler.jobstores.base import JobLookupError
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler, \
         patch("app.withdrawal_execution.remove_pending_withdrawal"), \
         patch("app.withdrawal_execution.append_withdrawal_audit"):
        mock_scheduler.remove_job.side_effect = JobLookupError("withdrawal_wd-aaaa1111")
        result = await cancel_pending_withdrawal("wd-aaaa1111")

    assert result["id"] == "wd-aaaa1111"
```

- [ ] **Step 11: Run the tests to verify they fail, then pass**

Run: `python -m pytest tests/test_withdrawal_execution.py -v -k cancel_pending_withdrawal`
Expected (before any further changes — the functions already exist from Step 8): PASS (3 tests). If any fail, fix `cancel_pending_withdrawal` to match, then re-run.

- [ ] **Step 12: Run the full test file**

Run: `python -m pytest tests/test_withdrawal_execution.py -v`
Expected: PASS (11 tests total)

- [ ] **Step 13: Commit**

```bash
git add app/withdrawal_execution.py app/config.py .env.example tests/test_withdrawal_execution.py
git commit -m "feat: add schedule/execute/cancel withdrawal execution module"
```

---

### Task 3: Startup rescheduling for pending withdrawals

**Files:**
- Modify: `app/scheduler.py`
- Modify: `app/main.py` (lifespan startup hook)
- Test: `tests/test_scheduler_withdrawals.py`

**Interfaces:**
- Consumes (from Task 1): `load_pending_withdrawals` from `app.pending_withdrawals`.
- Consumes (from Task 2): `execute_pending_withdrawal` from `app.withdrawal_execution`.
- Produces: `reschedule_pending_withdrawals() -> None` in `app.scheduler`, called from `main.py`'s lifespan startup alongside the existing `reschedule_pending_orders()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler_withdrawals.py`:

```python
import os
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

import pytz

ET = pytz.timezone("America/New_York")


def test_reschedule_pending_withdrawals_does_nothing_when_none_pending():
    from app.scheduler import reschedule_pending_withdrawals
    with patch("app.scheduler.load_pending_withdrawals", return_value=[]), \
         patch("app.scheduler.scheduler") as mock_scheduler:
        reschedule_pending_withdrawals()
    mock_scheduler.add_job.assert_not_called()


def test_reschedule_pending_withdrawals_adds_a_job_per_record():
    from app.scheduler import reschedule_pending_withdrawals
    future = (datetime.now(ET) + timedelta(hours=5)).isoformat()
    records = [
        {"id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
         "requested_at": "2026-06-21T10:00:00-05:00", "run_at": future},
    ]
    with patch("app.scheduler.load_pending_withdrawals", return_value=records), \
         patch("app.scheduler.scheduler") as mock_scheduler:
        reschedule_pending_withdrawals()

    mock_scheduler.add_job.assert_called_once()
    _, kwargs = mock_scheduler.add_job.call_args
    assert kwargs["id"] == "withdrawal_wd-aaaa1111"
    assert kwargs["args"] == ["wd-aaaa1111"]
    assert kwargs["replace_existing"] is True


def test_reschedule_pending_withdrawals_uses_now_when_run_at_already_passed():
    from app.scheduler import reschedule_pending_withdrawals
    past = (datetime.now(ET) - timedelta(hours=2)).isoformat()
    records = [
        {"id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
         "requested_at": "2026-06-20T10:00:00-05:00", "run_at": past},
    ]
    with patch("app.scheduler.load_pending_withdrawals", return_value=records), \
         patch("app.scheduler.scheduler") as mock_scheduler:
        reschedule_pending_withdrawals()

    _, kwargs = mock_scheduler.add_job.call_args
    assert kwargs["run_date"] >= datetime.now(ET) - timedelta(seconds=5)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_scheduler_withdrawals.py -v`
Expected: FAIL with `ImportError: cannot import name 'reschedule_pending_withdrawals'`

- [ ] **Step 3: Implement `reschedule_pending_withdrawals` in `app/scheduler.py`**

Add this import near the top of `app/scheduler.py`, alongside the existing imports (after `from app.rh_pnl import ...` at line 19):

```python
from app.pending_withdrawals import load_pending_withdrawals
```

Add this function after `reschedule_pending_orders()` (which ends at line 233):

```python
def reschedule_pending_withdrawals() -> None:
    from app.withdrawal_execution import execute_pending_withdrawal

    records = load_pending_withdrawals()
    if not records:
        return

    now = datetime.now(ET)
    for record in records:
        withdrawal_id = record["id"]
        try:
            run_dt = datetime.fromisoformat(record["run_at"])
            if run_dt.tzinfo is None:
                run_dt = ET.localize(run_dt)
        except Exception:
            run_dt = now

        effective_run = run_dt if run_dt > now else now

        scheduler.add_job(
            execute_pending_withdrawal,
            "date",
            run_date=effective_run,
            args=[withdrawal_id],
            id=f"withdrawal_{withdrawal_id}",
            replace_existing=True,
        )
        log.info("Rescheduled pending withdrawal %s for %s", withdrawal_id, effective_run)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_scheduler_withdrawals.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Wire the startup hook into `main.py`**

In `app/main.py`, change the import line (currently line 64):

```python
from app.scheduler import scheduler, setup_jobs, reschedule_pending_orders
```

to:

```python
from app.scheduler import scheduler, setup_jobs, reschedule_pending_orders, reschedule_pending_withdrawals
```

And in the lifespan function, change (currently lines 119-120):

```python
    setup_jobs()
    reschedule_pending_orders()
```

to:

```python
    setup_jobs()
    reschedule_pending_orders()
    reschedule_pending_withdrawals()
```

- [ ] **Step 6: Manually verify the app still starts**

Run: `python -c "from app.main import app; print('OK')"`
Expected: prints `OK` with no import errors (this exercises every import added in this task).

- [ ] **Step 7: Commit**

```bash
git add app/scheduler.py app/main.py tests/test_scheduler_withdrawals.py
git commit -m "feat: reschedule pending withdrawals on app startup"
```

---

### Task 4: Discord `/withdraw` uses the delay, plus new `/cancel-withdrawal` command

**Files:**
- Modify: `app/discord_commands.py`
- Modify: `scripts/register_commands.py` (add `cancel-withdrawal` command schema)
- Modify: `tests/test_discord_commands.py` (existing withdraw tests change behavior)

**Interfaces:**
- Consumes (from Task 2): `schedule_withdrawal`, `cancel_pending_withdrawal`, `WithdrawalValidationError`, `WithdrawalNotFoundError` from `app.withdrawal_execution`.
- Produces: `handle_withdraw(investor_name: str, amount: float, token: str) -> None` (signature unchanged, behavior changes); new `handle_cancel_withdrawal(withdrawal_id: str, token: str) -> None`; `dispatch_command()` routes `"cancel-withdrawal"` to it.

- [ ] **Step 1: Update the existing withdraw tests to match the new behavior**

In `tests/test_discord_commands.py`, replace `test_handle_withdraw_success` and `test_handle_withdraw_exceeds_total` (lines 44-79) with:

```python
@pytest.mark.asyncio
async def test_handle_withdraw_schedules_instead_of_writing_immediately():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.save_investors") as mock_save, \
         patch("app.withdrawal_execution.scheduler"), \
         patch("app.withdrawal_execution.save_pending_withdrawal"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    mock_save.assert_not_called()  # investors.json is NOT written yet
    msg = mock_edit.call_args[0][1]
    assert "500" in msg
    assert "Moses" in msg
    assert "cancel-withdrawal" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_exceeds_total_reports_error_without_scheduling():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "exceeds" in msg
```

Also update `test_handle_withdraw_investor_not_found` (currently at line 82, asserts `"not found" in msg`) — its patch targets have the same problem as the two tests above, since investor lookup now happens inside `schedule_withdrawal()` rather than in `handle_withdraw()` directly. Replace its two `patch("app.discord_commands...")` lines with `patch("app.withdrawal_execution.load_investors", ...)` and `patch("app.withdrawal_execution.get_latest_price", ...)`, keeping `patch("app.discord_commands._edit_original", ...)` as the third patch (unchanged) and the rest of the test body (call + assertion) unchanged:

```python
@pytest.mark.asyncio
async def test_handle_withdraw_investor_not_found():
    with patch("app.withdrawal_execution.load_investors", return_value=[]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Ghost", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "not found" in msg
```

- [ ] **Step 2: Run the updated tests to verify they fail**

Run: `python -m pytest tests/test_discord_commands.py -v -k withdraw`
Expected: FAIL — `handle_withdraw` still writes immediately (old behavior), so `mock_save.assert_not_called()` fails.

- [ ] **Step 3: Rewrite `handle_withdraw` in `app/discord_commands.py`**

Replace the existing `handle_withdraw` function (lines 82-127) with:

```python
async def handle_withdraw(investor_name: str, amount: float, token: str) -> None:
    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError

    try:
        record = await schedule_withdrawal(investor_name, amount)
    except WithdrawalValidationError as exc:
        await _edit_original(token, f"❌ {exc}")
        return

    run_at_local = datetime.fromisoformat(record["run_at"]).astimezone(_CT)
    msg = (
        f"⏳ **Withdrawal Scheduled** — {record['investor']}\n"
        f"${record['amount']:,.2f} will be processed at "
        f"{run_at_local.strftime('%b %d, %Y %I:%M %p %Z')}.\n"
        f"Run `/cancel-withdrawal id={record['id']}` to cancel."
    )

    from app.notifications import notify_investors
    asyncio.create_task(notify_investors(msg))
    await _edit_original(token, msg)


async def handle_cancel_withdrawal(withdrawal_id: str, token: str) -> None:
    from app.withdrawal_execution import cancel_pending_withdrawal, WithdrawalNotFoundError

    try:
        record = await cancel_pending_withdrawal(withdrawal_id)
    except WithdrawalNotFoundError as exc:
        await _edit_original(token, f"❌ {exc}")
        return

    await _edit_original(
        token,
        f"✅ Canceled withdrawal `{withdrawal_id}` — ${record['amount']:,.2f} for {record['investor']}.",
    )
```

The new `handle_withdraw` calls `schedule_withdrawal()`, which does its own investor lookup, price fetch, and validation inside `app/withdrawal_execution.py` — none of `compute_withdrawal_lots`, `format_withdrawal_message`, `Withdrawal`, `save_investors`, `investors_lock`, `load_investors`, or `get_latest_price` are called directly from `discord_commands.py` for withdrawals anymore. `handle_deposit` (above it in the same file) still uses `load_investors`/`save_investors`/`Deposit` for deposits, so leave the existing import block at the top of the file (lines 11-20) exactly as-is — don't remove anything from it. No new imports are needed in `discord_commands.py` for this step; `scheduler` and `save_pending_withdrawal` are only ever referenced from inside `app/withdrawal_execution.py` (Task 2), which is why the tests in Step 1 patch `app.withdrawal_execution.*` rather than `app.discord_commands.*`.

- [ ] **Step 4: Run the updated tests to verify they pass**

Run: `python -m pytest tests/test_discord_commands.py -v -k withdraw`
Expected: PASS (3 tests: schedules, exceeds, investor_not_found)

- [ ] **Step 5: Write the failing test for `handle_cancel_withdrawal`**

Append to `tests/test_discord_commands.py`:

```python
@pytest.mark.asyncio
async def test_handle_cancel_withdrawal_success():
    record = {"id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
              "requested_at": "t1", "run_at": "t2"}
    with patch("app.withdrawal_execution.cancel_pending_withdrawal", new_callable=AsyncMock) as mock_cancel, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        mock_cancel.return_value = record
        from app.discord_commands import handle_cancel_withdrawal
        await handle_cancel_withdrawal("wd-aaaa1111", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "Canceled" in msg
    assert "500" in msg
    assert "Moses" in msg


@pytest.mark.asyncio
async def test_handle_cancel_withdrawal_not_found():
    from app.withdrawal_execution import WithdrawalNotFoundError
    with patch("app.withdrawal_execution.cancel_pending_withdrawal", new_callable=AsyncMock) as mock_cancel, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        mock_cancel.side_effect = WithdrawalNotFoundError("No pending withdrawal with id wd-missing")
        from app.discord_commands import handle_cancel_withdrawal
        await handle_cancel_withdrawal("wd-missing", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "No pending withdrawal" in msg
```

- [ ] **Step 6: Run the tests — they should already pass**

Run: `python -m pytest tests/test_discord_commands.py -v -k cancel_withdrawal`
Expected: PASS (2 tests) — `handle_cancel_withdrawal` was already implemented in Step 3.

- [ ] **Step 7: Route `cancel-withdrawal` in `dispatch_command`**

In `app/discord_commands.py`, in `dispatch_command()` (currently lines 419-477), add a new branch right after the `elif command == "withdraw":` block (after line 433):

```python
        elif command == "cancel-withdrawal":
            await handle_cancel_withdrawal(withdrawal_id=options["id"], token=token)
```

Note: `handle_cancel_withdrawal`'s parameter is named `withdrawal_id`, but `dispatch_command` is calling it with `withdrawal_id=options["id"]` as a keyword argument — confirm the parameter name in the Step 3 definition is `withdrawal_id`, not `id` (it is, per the Interfaces block above).

- [ ] **Step 8: Register the new Discord command schema**

In `scripts/register_commands.py`, add a new entry to the `COMMANDS` list, after the `"withdraw"` entry (after line 60):

```python
    {
        "name": "cancel-withdrawal",
        "description": "Cancel a pending withdrawal before it executes",
        "options": [
            {
                "name": "id",
                "description": "Pending withdrawal ID (from the /withdraw confirmation message)",
                "type": 3,
                "required": True,
            },
        ],
    },
```

- [ ] **Step 9: Run the full discord_commands test file**

Run: `python -m pytest tests/test_discord_commands.py -v`
Expected: PASS (all tests, including the pre-existing deposit tests which are untouched)

- [ ] **Step 10: Commit**

```bash
git add app/discord_commands.py scripts/register_commands.py tests/test_discord_commands.py
git commit -m "feat: delay /withdraw via scheduling, add /cancel-withdrawal command"
```

---

### Task 5: `/pending-withdrawals` read-only listing command

**Files:**
- Modify: `app/discord_commands.py`
- Modify: `scripts/register_commands.py`
- Modify: `tests/test_discord_commands.py`

**Interfaces:**
- Consumes (from Task 1): `load_pending_withdrawals` from `app.pending_withdrawals`.
- Produces: `handle_pending_withdrawals(token: str) -> None`; `dispatch_command()` routes `"pending-withdrawals"` to it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_discord_commands.py`:

```python
@pytest.mark.asyncio
async def test_handle_pending_withdrawals_lists_all_pending():
    records = [
        {"id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
         "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00"},
        {"id": "wd-bbbb2222", "investor": "Gabe", "amount": 200.0,
         "requested_at": "2026-06-21T11:00:00-05:00", "run_at": "2026-06-22T11:00:00-05:00"},
    ]
    with patch("app.discord_commands.load_pending_withdrawals", return_value=records), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_pending_withdrawals
        await handle_pending_withdrawals("test-token")

    msg = mock_edit.call_args[0][1]
    assert "wd-aaaa1111" in msg
    assert "Moses" in msg
    assert "wd-bbbb2222" in msg
    assert "Gabe" in msg


@pytest.mark.asyncio
async def test_handle_pending_withdrawals_reports_none_pending():
    with patch("app.discord_commands.load_pending_withdrawals", return_value=[]), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_pending_withdrawals
        await handle_pending_withdrawals("test-token")

    msg = mock_edit.call_args[0][1]
    assert "No pending withdrawals" in msg
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_discord_commands.py -v -k pending_withdrawals`
Expected: FAIL with `ImportError: cannot import name 'handle_pending_withdrawals'`

- [ ] **Step 3: Implement `handle_pending_withdrawals`**

Add to `app/discord_commands.py`, after `handle_cancel_withdrawal` (added in Task 4 Step 3). Add this import alongside the existing `app.investors` import block:

```python
from app.pending_withdrawals import load_pending_withdrawals
```

```python
async def handle_pending_withdrawals(token: str) -> None:
    records = load_pending_withdrawals()
    if not records:
        await _edit_original(token, "No pending withdrawals.")
        return

    lines = ["⏳ **Pending Withdrawals**", ""]
    for r in records:
        run_at_local = datetime.fromisoformat(r["run_at"]).astimezone(_CT)
        lines.append(
            f"`{r['id']}` — {r['investor']}: ${r['amount']:,.2f}"
            f" (scheduled {run_at_local.strftime('%b %d, %I:%M %p %Z')})"
        )
    await _edit_original(token, "\n".join(lines))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_discord_commands.py -v -k pending_withdrawals`
Expected: PASS (2 tests)

- [ ] **Step 5: Route `pending-withdrawals` in `dispatch_command`**

In `app/discord_commands.py`, in `dispatch_command()`, add after the `cancel-withdrawal` branch added in Task 4 Step 7:

```python
        elif command == "pending-withdrawals":
            await handle_pending_withdrawals(token=token)
```

- [ ] **Step 6: Register the new Discord command schema**

In `scripts/register_commands.py`, add after the `cancel-withdrawal` entry added in Task 4 Step 8:

```python
    {
        "name": "pending-withdrawals",
        "description": "List withdrawals currently waiting out their delay window",
        "options": [],
    },
```

- [ ] **Step 7: Run the full discord_commands test file**

Run: `python -m pytest tests/test_discord_commands.py -v`
Expected: PASS (all tests)

- [ ] **Step 8: Commit**

```bash
git add app/discord_commands.py scripts/register_commands.py tests/test_discord_commands.py
git commit -m "feat: add /pending-withdrawals read-only listing command"
```

---

### Task 6: `POST /withdraw` HTTP endpoint uses the same delay mechanism

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_withdraw.py` (new — mirrors `tests/test_deposit.py`'s structure for the HTTP endpoint)

**Interfaces:**
- Consumes (from Task 2): `schedule_withdrawal`, `WithdrawalValidationError` from `app.withdrawal_execution`.
- Produces: `POST /withdraw` now returns `{"status": "scheduled", "id", "investor", "amount", "run_at"}` (HTTP 200) on success, instead of executing immediately and returning the final FIFO/tax breakdown.

This is the fix for the bypass identified during planning: this endpoint shares `verify_webhook_secret` with ~11 other endpoints and previously performed the same immediate write as the Discord command, with no delay at all.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_withdraw.py`:

```python
import os
import pytest
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from fastapi.testclient import TestClient

from app.investors import Deposit, Investor
from app.main import app

client = TestClient(app)
TEST_SECRET = "MY_SHARED_SECRET"


def _initial_investors():
    return [
        Investor(name="Moses", deposits=[Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09")])
    ]


def test_withdraw_rejects_wrong_secret():
    response = client.post("/withdraw", json={
        "secret": "wrong-secret",
        "investor": "Moses",
        "amount": 500.0,
    })
    assert response.status_code == 401


def test_withdraw_rejects_malformed_json():
    response = client.post(
        "/withdraw",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_withdraw_schedules_instead_of_writing_immediately():
    with patch("app.withdrawal_execution.load_investors", return_value=_initial_investors()), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.save_pending_withdrawal") as mock_save_pending, \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler:
        response = client.post("/withdraw", json={
            "secret": TEST_SECRET,
            "investor": "Moses",
            "amount": 500.0,
        })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "scheduled"
    assert data["investor"] == "Moses"
    assert data["amount"] == 500.0
    assert data["id"].startswith("wd-")
    # Proves this went through the delay mechanism, not a direct investors.json write:
    mock_save_pending.assert_called_once()
    mock_scheduler.add_job.assert_called_once()


def test_withdraw_returns_400_when_amount_exceeds_equity():
    with patch("app.withdrawal_execution.load_investors", return_value=_initial_investors()), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20):
        response = client.post("/withdraw", json={
            "secret": TEST_SECRET,
            "investor": "Moses",
            "amount": 50000.0,
        })
    assert response.status_code == 400
    assert "exceeds" in response.json()["error"]


def test_withdraw_returns_400_when_investor_not_found():
    with patch("app.withdrawal_execution.load_investors", return_value=_initial_investors()), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20):
        response = client.post("/withdraw", json={
            "secret": TEST_SECRET,
            "investor": "Ghost",
            "amount": 500.0,
        })
    assert response.status_code == 400
    assert "not found" in response.json()["error"]


def test_withdraw_rejects_zero_amount():
    response = client.post("/withdraw", json={
        "secret": TEST_SECRET,
        "investor": "Moses",
        "amount": 0.0,
    })
    assert response.status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_withdraw.py -v`
Expected: FAIL — current `/withdraw` still executes immediately and returns the old FIFO-breakdown response shape, so `data["status"] == "scheduled"` and `mock_save.assert_not_called()` fail.

- [ ] **Step 3: Rewrite the `/withdraw` endpoint in `app/main.py`**

Replace the entire existing `withdraw()` function — from its `@app.post("/withdraw", tags=["investors"])` decorator (line 776) through its final `return {...}` statement (around line 882, the one returning the `lots` breakdown) — with the following. The JSON-parsing, secret-verification, and `WithdrawRequest` validation logic is unchanged from today (same lines, just retyped here as part of the full replacement); only the code after validation (everything that used to compute and write the withdrawal) is new:

```python
@app.post("/withdraw", tags=["investors"])
async def withdraw(request: Request) -> dict:
    """
    Schedule a delayed cash withdrawal for an investor.

    Flow:
      1. Parse and validate request against WithdrawRequest.
      2. Verify webhook secret.
      3. Validate investor + amount via schedule_withdrawal() (same function
         the Discord /withdraw command uses) and schedule it to execute after
         the configured delay (WITHDRAWAL_DELAY_HOURS, default 24).
      4. Return the pending record. The actual investors.json write, FIFO
         breakdown, and Discord notification happen later, when the scheduled
         job fires (see app.withdrawal_execution.execute_pending_withdrawal).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Request body must be valid JSON."},
        )

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Request body must be a JSON object."},
        )

    verify_webhook_secret(body.get("secret", ""))

    try:
        req = WithdrawRequest(**body)
    except ValidationError as exc:
        def _serialisable(errors):
            result = []
            for err in errors:
                err = dict(err)
                if "ctx" in err:
                    err["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
                err.pop("url", None)
                result.append(err)
            return result

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "Invalid withdrawal request.", "detail": _serialisable(exc.errors())},
        )

    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError

    try:
        record = await schedule_withdrawal(req.investor, req.amount, spy_price=req.spy_price)
    except WithdrawalValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": str(exc)},
        )

    from app.notifications import notify_investors
    _fire(notify_investors(
        f"⏳ Withdrawal of ${record['amount']:,.2f} for {record['investor']} scheduled "
        f"for {record['run_at']}. Cancel via /cancel-withdrawal id={record['id']} in Discord."
    ))

    return {
        "status": "scheduled",
        "id": record["id"],
        "investor": record["investor"],
        "amount": record["amount"],
        "run_at": record["run_at"],
    }
```

Note: this removes the use of `compute_withdrawal_lots`, `format_withdrawal_message`, `Withdrawal`, `save_investors`, and `investors_lock` from this specific function — but `app/main.py` has other endpoints (e.g. `/deposit`, the `/investor-breakdown`-style report endpoints) that still use `load_investors`/`save_investors`/`Deposit`/`Withdrawal` from the same top-level import block (lines 36-45). Leave that import block exactly as-is; don't remove anything from it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_withdraw.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `python -m pytest tests/ -v`
Expected: PASS — all tests across the project, including the untouched `/deposit` tests, `test_investors.py`, `test_discord_commands.py`, `test_pending_withdrawals.py`, `test_withdrawal_audit.py`, `test_withdrawal_execution.py`, `test_scheduler_withdrawals.py`.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_withdraw.py
git commit -m "feat: route POST /withdraw through the same delayed-withdrawal mechanism as Discord"
```

---

### Task 7: Manual end-to-end smoke check

**Files:** none (no code changes — verification only)

**Interfaces:** none

This task has no automated test of its own; it's a manual check that the full chain works together with a real (very short) delay, since every other task tested its piece in isolation with mocks.

- [ ] **Step 1: Run a local smoke test with a near-zero delay**

```bash
WITHDRAWAL_DELAY_HOURS=0 ALPACA_API_KEY=test ALPACA_SECRET_KEY=test WEBHOOK_SECRET=test123 \
  INVESTORS_PATH=/tmp/smoke_investors.json \
  PENDING_WITHDRAWALS_PATH=/tmp/smoke_pending.json \
  WITHDRAWAL_AUDIT_PATH=/tmp/smoke_audit.json \
  python -c "
import asyncio, json
from pathlib import Path

Path('/tmp/smoke_investors.json').write_text(json.dumps({
    'investors': [{'name': 'Smoke', 'deposits': [{'amount': 1000.0, 'entry_spy': 700.0, 'date': '2026-01-01'}], 'withdrawals': []}]
}))

from app.withdrawal_execution import schedule_withdrawal, execute_pending_withdrawal
from unittest.mock import patch

async def main():
    with patch('app.withdrawal_execution.get_latest_price', return_value=710.0), \
         patch('app.scheduler.scheduler.add_job'):
        record = await schedule_withdrawal('Smoke', 100.0)
        print('Scheduled:', record)

    pending = json.loads(Path('/tmp/smoke_pending.json').read_text())
    print('Pending file:', pending)
    assert len(pending['pending']) == 1

    with patch('app.withdrawal_execution.get_latest_price', return_value=710.0), \
         patch('app.withdrawal_execution.notify_investors', return_value=asyncio.sleep(0)), \
         patch('app.withdrawal_execution.push_backup', return_value=asyncio.sleep(0)):
        await execute_pending_withdrawal(record['id'])

    investors = json.loads(Path('/tmp/smoke_investors.json').read_text())
    print('Final investors.json:', investors)
    assert len(investors['investors'][0]['withdrawals']) == 1

    pending_after = json.loads(Path('/tmp/smoke_pending.json').read_text())
    assert pending_after['pending'] == []

    audit = json.loads(Path('/tmp/smoke_audit.json').read_text())
    print('Audit log:', audit)
    assert audit['audit'][0]['status'] == 'executed'

    print('SMOKE TEST PASSED')

asyncio.run(main())
"
```

Expected output ends with `SMOKE TEST PASSED`, and the three intermediate prints show: a `Scheduled:` record with a `wd-` id, a `Pending file:` with one entry, a `Final investors.json:` with one withdrawal recorded for Smoke, and an `Audit log:` entry with `status: executed`.

- [ ] **Step 2: Clean up smoke-test files**

```bash
rm -f /tmp/smoke_investors.json /tmp/smoke_investors.tmp /tmp/smoke_pending.json /tmp/smoke_pending.tmp /tmp/smoke_audit.json /tmp/smoke_audit.tmp
```

- [ ] **Step 3: Report results to the user**

No commit for this task — it's verification only. Report the smoke test output and confirm all prior tasks' automated tests still pass (`python -m pytest tests/ -v`).
