# Alpaca Hedge-Fund Trade Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll Alpaca for filled non-SPY trades, notify Discord on entry/exit with win/loss and per-investor P&L split, post a midnight-CT daily recap, and add a non-SPY contribution line to the investor breakdown.

**Architecture:** A new record module (`alpaca_hf_record.py`) holds FIFO open-lot state and closed-trade history on the Render persistent disk. A new notifier module (`alpaca_hf_notifier.py`) polls `get_orders_filled_range`, classifies long/short via `position_intent`, pairs round trips FIFO, and posts messages. Two APScheduler jobs drive it: a 2-minute poll and a 00:00 CT recap. `notifications.py`, `config.py`, and the investor breakdown get small additive edits.

**Tech Stack:** Python 3.12, FastAPI, APScheduler, alpaca-py, pytest. Deployed on Render, persistent disk at `/data/`.

**Spec:** `docs/superpowers/specs/2026-08-27-alpaca-hf-trade-notifier-design.md`

## Global Constraints

- Python 3.12; async throughout.
- State files on `/data/`, atomic tmp-write-then-`replace`, guarded by an `asyncio.Lock` (mirror `app/rh_trade_record.py`).
- Money format `$1,234.56`; times displayed in `America/Chicago` (CT).
- New config fields are `Optional[str] = None`; each notifier no-ops with a logged warning when its webhook is unset (mirror `notify_trades`).
- Exclude `symbol == "SPY"` and any order whose `status != FILLED` or `filled_qty == 0`.
- Tests set `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`/`WEBHOOK_SECRET` env (see `tests/conftest.py`) and patch state-file paths to `tmp_path`.
- Run tests with `python -m pytest`.

---

### Task 1: Config — two new webhook fields

**Files:**
- Modify: `app/config.py` (add fields near existing `discord_trades_webhook_url:39`)
- Test: `tests/test_config_hf.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.alpaca_hf_trades_webhook_url: Optional[str]`, `settings.alpaca_hf_recap_webhook_url: Optional[str]` (both default `None`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_hf.py
import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def test_hf_webhook_fields_default_none():
    from app.config import Settings
    s = Settings()
    assert s.alpaca_hf_trades_webhook_url is None
    assert s.alpaca_hf_recap_webhook_url is None


def test_hf_webhook_fields_read_env(monkeypatch):
    monkeypatch.setenv("ALPACA_HF_TRADES_WEBHOOK_URL", "https://x/trades")
    monkeypatch.setenv("ALPACA_HF_RECAP_WEBHOOK_URL", "https://x/recap")
    from app.config import Settings
    s = Settings()
    assert s.alpaca_hf_trades_webhook_url == "https://x/trades"
    assert s.alpaca_hf_recap_webhook_url == "https://x/recap"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_hf.py -v`
Expected: FAIL (`AttributeError` on `alpaca_hf_trades_webhook_url`).

- [ ] **Step 3: Add the fields**

In `app/config.py`, after the `discord_trades_webhook_url` field:

```python
    alpaca_hf_trades_webhook_url: Optional[str] = None
    alpaca_hf_recap_webhook_url: Optional[str] = None
```

pydantic-settings maps the field names to the upper-case env vars automatically.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_hf.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config_hf.py
git commit -m "feat: add Alpaca HF webhook config fields"
```

---

### Task 2: Record module — FIFO pairing, dedup, and state

**Files:**
- Create: `app/alpaca_hf_record.py`
- Test: `tests/test_alpaca_hf_record.py` (create)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (all async unless noted; `_STATE_FILE: Path` module attribute patched in tests):
  - `async get_last_seen() -> datetime | None`
  - `async set_last_seen(dt: datetime) -> None`
  - `async is_seen(order_id: str) -> bool`
  - `async mark_seen(order_id: str) -> None`
  - `async record_open(symbol: str, direction: str, qty: float, price: float, ts: str, order_id: str) -> None`
  - `async record_close(symbol: str, direction: str, qty: float, exit_price: float, ts: str) -> CloseResult`
  - `async record_daily_fill(fill: dict) -> None`
  - `async pop_daily_fills() -> list[dict]`
  - `async contribution_total() -> float`
  - `CloseResult` = dataclass `{matched_qty: float, realized_pnl: float, pct: float, is_win: bool | None, unmatched_qty: float}` (`is_win=None` when nothing matched).
  - `direction` is the string `"LONG"` or `"SHORT"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_alpaca_hf_record.py
import os
import pytest
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    import app.alpaca_hf_record as rec
    monkeypatch.setattr(rec, "_STATE_FILE", tmp_path / "hf.json")
    yield


@pytest.mark.asyncio
async def test_long_roundtrip_win():
    import app.alpaca_hf_record as rec
    await rec.record_open("QCOM", "LONG", 12, 164.37, "t1", "o1")
    r = await rec.record_close("QCOM", "LONG", 12, 164.84, "t2")
    assert r.matched_qty == 12
    assert round(r.realized_pnl, 2) == 5.64
    assert r.is_win is True
    assert r.unmatched_qty == 0


@pytest.mark.asyncio
async def test_short_roundtrip_loss():
    import app.alpaca_hf_record as rec
    # sell_to_open @112.18, buy_to_close @112.2469 -> short loses
    await rec.record_open("CSCO", "SHORT", 17, 112.18, "t1", "o1")
    r = await rec.record_close("CSCO", "SHORT", 17, 112.2469, "t2")
    assert r.realized_pnl < 0
    assert r.is_win is False


@pytest.mark.asyncio
async def test_partial_close_leaves_lot():
    import app.alpaca_hf_record as rec
    await rec.record_open("AMZN", "LONG", 7, 256.80, "t1", "o1")
    r = await rec.record_close("AMZN", "LONG", 4, 257.40, "t2")
    assert r.matched_qty == 4
    assert r.unmatched_qty == 0
    r2 = await rec.record_close("AMZN", "LONG", 3, 257.40, "t3")
    assert r2.matched_qty == 3


@pytest.mark.asyncio
async def test_close_without_open_is_neutral():
    import app.alpaca_hf_record as rec
    r = await rec.record_close("TSLA", "LONG", 5, 353.05, "t1")
    assert r.matched_qty == 0
    assert r.is_win is None
    assert r.unmatched_qty == 5


@pytest.mark.asyncio
async def test_dedup_and_last_seen_persist():
    import app.alpaca_hf_record as rec
    from datetime import datetime, timezone
    assert await rec.is_seen("o1") is False
    await rec.mark_seen("o1")
    assert await rec.is_seen("o1") is True
    dt = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)
    await rec.set_last_seen(dt)
    assert await rec.get_last_seen() == dt


@pytest.mark.asyncio
async def test_daily_fills_buffer_pop_clears():
    import app.alpaca_hf_record as rec
    await rec.record_daily_fill({"symbol": "QCOM", "role": "OPEN"})
    fills = await rec.pop_daily_fills()
    assert len(fills) == 1
    assert await rec.pop_daily_fills() == []


@pytest.mark.asyncio
async def test_contribution_total_sums_closed():
    import app.alpaca_hf_record as rec
    await rec.record_open("QCOM", "LONG", 12, 164.37, "t1", "o1")
    await rec.record_close("QCOM", "LONG", 12, 164.84, "t2")
    assert round(await rec.contribution_total(), 2) == 5.64
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_alpaca_hf_record.py -v`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement the record module**

```python
# app/alpaca_hf_record.py
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_STATE_FILE = Path(os.getenv("ALPACA_HF_RECORD_PATH", "/data/alpaca_hf_record.json"))
_lock = asyncio.Lock()
_MAX_SEEN = 2000


@dataclass
class CloseResult:
    matched_qty: float
    realized_pnl: float
    pct: float
    is_win: Optional[bool]
    unmatched_qty: float


def _empty() -> dict:
    return {
        "last_seen": None,
        "seen_order_ids": [],
        "open_lots": {},
        "closed_trades": [],
        "daily_fills": [],
        "wins": 0,
        "losses": 0,
    }


def _load() -> dict:
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text())
            base = _empty()
            base.update(data)
            return base
        except Exception:
            log.exception("Corrupt HF state; starting fresh")
    return _empty()


def _save(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(_STATE_FILE)


async def get_last_seen() -> Optional[datetime]:
    async with _lock:
        raw = _load().get("last_seen")
    return datetime.fromisoformat(raw) if raw else None


async def set_last_seen(dt: datetime) -> None:
    async with _lock:
        state = _load()
        state["last_seen"] = dt.isoformat()
        _save(state)


async def is_seen(order_id: str) -> bool:
    async with _lock:
        return order_id in _load().get("seen_order_ids", [])


async def mark_seen(order_id: str) -> None:
    async with _lock:
        state = _load()
        ids = state["seen_order_ids"]
        if order_id not in ids:
            ids.append(order_id)
            if len(ids) > _MAX_SEEN:
                del ids[: len(ids) - _MAX_SEEN]
            _save(state)


async def record_open(symbol, direction, qty, price, ts, order_id) -> None:
    async with _lock:
        state = _load()
        state["open_lots"].setdefault(symbol, []).append({
            "direction": direction, "qty": float(qty),
            "entry_price": float(price), "entry_ts": ts, "order_id": order_id,
        })
        _save(state)


async def record_close(symbol, direction, qty, exit_price, ts) -> CloseResult:
    async with _lock:
        state = _load()
        lots = state["open_lots"].get(symbol, [])
        remaining = float(qty)
        matched = 0.0
        pnl = 0.0
        cost = 0.0
        kept: list = []
        for lot in lots:
            if remaining <= 0 or lot["direction"] != direction:
                kept.append(lot)
                continue
            take = min(lot["qty"], remaining)
            entry = lot["entry_price"]
            if direction == "LONG":
                pnl += (exit_price - entry) * take
            else:
                pnl += (entry - exit_price) * take
            cost += entry * take
            matched += take
            remaining -= take
            leftover = lot["qty"] - take
            if leftover > 1e-9:
                lot = {**lot, "qty": leftover}
                kept.append(lot)
        state["open_lots"][symbol] = kept

        if matched <= 0:
            _save(state)
            return CloseResult(0.0, 0.0, 0.0, None, remaining)

        pct = (pnl / cost * 100) if cost else 0.0
        is_win = pnl > 0
        state["closed_trades"].append({
            "symbol": symbol, "direction": direction, "qty": matched,
            "exit_price": exit_price, "realized_pnl": round(pnl, 4),
            "pct": round(pct, 4), "is_win": is_win, "closed_ts": ts,
        })
        if is_win:
            state["wins"] += 1
        else:
            state["losses"] += 1
        _save(state)
        return CloseResult(matched, pnl, pct, is_win, remaining)


async def record_daily_fill(fill: dict) -> None:
    async with _lock:
        state = _load()
        state["daily_fills"].append(fill)
        _save(state)


async def pop_daily_fills() -> list:
    async with _lock:
        state = _load()
        fills = state["daily_fills"]
        state["daily_fills"] = []
        _save(state)
    return fills


async def contribution_total() -> float:
    async with _lock:
        return sum(t["realized_pnl"] for t in _load().get("closed_trades", []))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_alpaca_hf_record.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/alpaca_hf_record.py tests/test_alpaca_hf_record.py
git commit -m "feat: add Alpaca HF record with FIFO round-trip pairing"
```

---

### Task 3: Notification senders

**Files:**
- Modify: `app/notifications.py` (add after `notify_trades:88`)
- Test: `tests/test_notifications_hf.py` (create)

**Interfaces:**
- Consumes: `settings.alpaca_hf_trades_webhook_url`, `settings.alpaca_hf_recap_webhook_url` (Task 1).
- Produces: `async notify_hf_trade(message: str) -> None`, `async notify_hf_recap(message: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notifications_hf.py
import os
import pytest
from unittest.mock import AsyncMock, patch
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


@pytest.mark.asyncio
async def test_notify_hf_trade_posts_to_webhook():
    import app.notifications as n
    client = AsyncMock()
    with patch.object(n.settings, "alpaca_hf_trades_webhook_url", "https://x/trades"), \
         patch.object(n, "get_http_client", return_value=client):
        await n.notify_hf_trade("hello")
    client.post.assert_awaited_once()
    assert client.post.await_args.args[0] == "https://x/trades"


@pytest.mark.asyncio
async def test_notify_hf_recap_noop_when_unset():
    import app.notifications as n
    client = AsyncMock()
    with patch.object(n.settings, "alpaca_hf_recap_webhook_url", None), \
         patch.object(n, "get_http_client", return_value=client):
        await n.notify_hf_recap("hello")
    client.post.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notifications_hf.py -v`
Expected: FAIL (`notify_hf_trade` undefined).

- [ ] **Step 3: Implement the senders**

Copy the structure of the existing `notify_trades`. Add to `app/notifications.py`:

```python
async def notify_hf_trade(message: str) -> None:
    url = settings.alpaca_hf_trades_webhook_url
    if not url:
        log.warning("ALPACA_HF_TRADES_WEBHOOK_URL not set; skipping HF trade notification")
        return
    try:
        await get_http_client().post(url, json={"content": message[:1990]}, timeout=10)
    except Exception as exc:
        log.warning("Failed to send HF trade notification: %s", exc)


async def notify_hf_recap(message: str) -> None:
    url = settings.alpaca_hf_recap_webhook_url
    if not url:
        log.warning("ALPACA_HF_RECAP_WEBHOOK_URL not set; skipping HF recap notification")
        return
    try:
        await get_http_client().post(url, json={"content": message[:1990]}, timeout=10)
    except Exception as exc:
        log.warning("Failed to send HF recap notification: %s", exc)
```

(Confirm the exact post signature by matching the real `notify_trades` body; keep chunking behavior identical if it splits long messages.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notifications_hf.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/notifications.py tests/test_notifications_hf.py
git commit -m "feat: add HF trade + recap Discord senders"
```

---

### Task 4: Message + recap formatters (pure functions)

**Files:**
- Create: `app/alpaca_hf_notifier.py` (formatters only in this task)
- Test: `tests/test_alpaca_hf_format.py` (create)

**Interfaces:**
- Consumes: `CloseResult` shape (Task 2) conceptually; formatters take plain args.
- Produces:
  - `format_open(symbol, direction, qty, price, ts_ct: datetime) -> str`
  - `format_close(symbol, direction, qty, exit_price, realized_pnl, pct, is_win, investor_split: list[tuple[str, float]], ts_ct: datetime) -> str` (`is_win=None` renders `P&L: n/a (no recorded entry)` and no split)
  - `format_recap(day_label: str, fills: list[dict], wins: int, losses: int, total_pnl: float) -> str`
  - `_money(x: float) -> str` and `_signed(x: float) -> str` helpers.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_alpaca_hf_format.py
import os
from datetime import datetime
import pytz
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
CT = pytz.timezone("America/Chicago")


def test_format_open_has_shares_and_notional():
    from app.alpaca_hf_notifier import format_open
    ts = CT.localize(datetime(2026, 8, 27, 15, 32))
    msg = format_open("QCOM", "LONG", 12, 164.77, ts)
    assert "LONG OPEN" in msg and "QCOM" in msg
    assert "12" in msg and "$164.77" in msg
    assert "$1,977.24" in msg


def test_format_close_win_shows_pnl_and_split():
    from app.alpaca_hf_notifier import format_close
    ts = CT.localize(datetime(2026, 8, 27, 10, 11))
    msg = format_close("QCOM", "LONG", 12, 164.84, 5.64, 0.29, True,
                       [("Alice", 3.10), ("Bob", 2.54)], ts)
    assert "WIN" in msg
    assert "+$5.64" in msg and "+0.29%" in msg
    assert "Alice" in msg and "+$3.10" in msg


def test_format_close_no_entry_is_na():
    from app.alpaca_hf_notifier import format_close
    ts = CT.localize(datetime(2026, 8, 27, 10, 11))
    msg = format_close("TSLA", "LONG", 5, 353.05, 0.0, 0.0, None, [], ts)
    assert "n/a" in msg


def test_format_recap_counts_and_winrate():
    from app.alpaca_hf_notifier import format_recap
    fills = [{"symbol": "QCOM", "role": "OPEN", "direction": "LONG",
              "qty": 12, "price": 164.37, "notional": 1972.44},
             {"symbol": "QCOM", "role": "CLOSE", "direction": "LONG",
              "qty": 12, "price": 164.84, "realized_pnl": 5.64}]
    msg = format_recap("August 27, 2026", fills, wins=5, losses=2, total_pnl=41.28)
    assert "5 W" in msg and "2 L" in msg
    assert "71.4" in msg
    assert "+$41.28" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_alpaca_hf_format.py -v`
Expected: FAIL (module/functions undefined).

- [ ] **Step 3: Implement the formatters**

```python
# app/alpaca_hf_notifier.py  (formatters; poller added in Task 5)
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz

log = logging.getLogger(__name__)
CT = pytz.timezone("America/Chicago")

_GREEN = "\U0001F7E2"  # green circle
_RED = "\U0001F534"    # red circle


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _time_ct(ts_ct: datetime) -> str:
    hour = int(ts_ct.strftime("%I"))
    return f"{hour}:{ts_ct.strftime('%M %p')} {ts_ct.strftime('%Z')} — {ts_ct.strftime('%B')} {ts_ct.day}, {ts_ct.year}"


def format_open(symbol, direction, qty, price, ts_ct: datetime) -> str:
    emoji = _GREEN if direction == "LONG" else _RED
    notional = price * qty
    return "\n".join([
        f"{emoji} **{direction} OPEN — {symbol}**",
        f"{qty:g} shares @ {_money(price)} ({_money(notional)})",
        f"\U0001F550 {_time_ct(ts_ct)}",
    ])


def format_close(symbol, direction, qty, exit_price, realized_pnl, pct,
                 is_win: Optional[bool], investor_split, ts_ct: datetime) -> str:
    notional = exit_price * qty
    if is_win is None:
        verdict = ""
        pnl_line = "P&L: n/a (no recorded entry)"
        split_lines = []
    else:
        verdict = "  WIN" if is_win else "  LOSS"
        pnl_line = f"P&L: {_signed(realized_pnl)} ({'+' if pct >= 0 else '-'}{abs(pct):.2f}%)"
        split_lines = ["Investor split:"] + [
            f"  - {name}: {_signed(amt)}" for name, amt in investor_split
        ]
    head_emoji = _GREEN if (is_win or direction == "LONG") else _RED
    lines = [
        f"{head_emoji} **{direction} CLOSE — {symbol}**{verdict}",
        f"Exit: {qty:g} shares @ {_money(exit_price)} ({_money(notional)})",
        pnl_line,
        f"\U0001F550 {_time_ct(ts_ct)}",
    ] + split_lines
    return "\n".join(lines)


def format_recap(day_label, fills, wins, losses, total_pnl) -> str:
    opens = sum(1 for f in fills if f.get("role") == "OPEN")
    closes = sum(1 for f in fills if f.get("role") == "CLOSE")
    total = wins + losses
    win_rate = (wins / total * 100) if total else 0.0
    lines = [
        f"**Non-SPY Recap — {day_label} (CT)**",
        f"Fills today: {len(fills)}  ({opens} opens / {closes} closes)",
        f"Closed round-trips: {total} — {wins} W / {losses} L ({win_rate:.1f}% win rate)",
        f"Total realized P&L: {_signed(total_pnl)}",
        "Fills:",
    ]
    for f in fills:
        if f.get("role") == "CLOSE" and "realized_pnl" in f:
            tail = f"({_signed(f['realized_pnl'])})"
        else:
            tail = f"({_money(f.get('notional', 0.0))})"
        lines.append(
            f"  {f['symbol']} {f.get('direction','')} {f.get('role','')} "
            f"{f.get('qty', 0):g} @ {_money(f.get('price', 0.0))} {tail}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_alpaca_hf_format.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/alpaca_hf_notifier.py tests/test_alpaca_hf_format.py
git commit -m "feat: add HF open/close/recap message formatters"
```

---

### Task 5: Poller + recap orchestration

**Files:**
- Modify: `app/alpaca_hf_notifier.py` (add `poll_and_notify`, `send_daily_recap`, `_classify`, `_investor_split`)
- Test: `tests/test_alpaca_hf_notifier.py` (create)

**Interfaces:**
- Consumes: `get_orders_filled_range` (`app/trading/alpaca_client.py:254`), `get_account` (`app/trading/alpaca_client.py:100`), `alpaca_hf_record` funcs (Task 2), `format_*` (Task 4), `notify_hf_trade`/`notify_hf_recap` (Task 3), `compute_breakdown` + `load_investors` (`app/investors.py`).
- Produces:
  - `_classify(order) -> tuple[str, str] | None` returning `(direction, role)` with direction in `{"LONG","SHORT"}`, role in `{"OPEN","CLOSE"}`; `None` if unclassifiable.
  - `async poll_and_notify() -> None`
  - `async send_daily_recap() -> None`
  - `async _investor_split(realized_pnl: float) -> list[tuple[str, float]]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_alpaca_hf_notifier.py
import os
from types import SimpleNamespace
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, patch
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def _order(symbol, side, intent, qty, price, oid):
    return SimpleNamespace(
        symbol=symbol, side=SimpleNamespace(value=side),
        position_intent=SimpleNamespace(value=intent),
        filled_qty=str(qty), filled_avg_price=str(price),
        id=oid, filled_at=datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc),
    )


def test_classify_table():
    from app.alpaca_hf_notifier import _classify
    assert _classify(_order("QCOM", "buy", "buy_to_open", 1, 1, "a")) == ("LONG", "OPEN")
    assert _classify(_order("QCOM", "sell", "sell_to_close", 1, 1, "b")) == ("LONG", "CLOSE")
    assert _classify(_order("CSCO", "sell", "sell_to_open", 1, 1, "c")) == ("SHORT", "OPEN")
    assert _classify(_order("CSCO", "buy", "buy_to_close", 1, 1, "d")) == ("SHORT", "CLOSE")


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    import app.alpaca_hf_record as rec
    monkeypatch.setattr(rec, "_STATE_FILE", tmp_path / "hf.json")
    yield


@pytest.mark.asyncio
async def test_poll_seeds_first_run_no_backfill():
    import app.alpaca_hf_notifier as nf
    orders = [_order("QCOM", "buy", "buy_to_open", 12, 164.37, "q1")]
    with patch.object(nf, "get_orders_filled_range", return_value=orders), \
         patch.object(nf, "notify_hf_trade", new=AsyncMock()) as post:
        await nf.poll_and_notify()  # first run: seeds last_seen, no notify
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_poll_skips_spy_and_notifies_open():
    import app.alpaca_hf_notifier as nf
    import app.alpaca_hf_record as rec
    from datetime import datetime, timezone, timedelta
    await rec.set_last_seen(datetime.now(timezone.utc) - timedelta(hours=1))
    orders = [
        _order("SPY", "buy", "buy_to_open", 1, 770, "spy1"),
        _order("QCOM", "buy", "buy_to_open", 12, 164.37, "q1"),
    ]
    with patch.object(nf, "get_orders_filled_range", return_value=orders), \
         patch.object(nf, "notify_hf_trade", new=AsyncMock()) as post:
        await nf.poll_and_notify()
    assert post.await_count == 1
    assert "QCOM" in post.await_args.args[0]


@pytest.mark.asyncio
async def test_poll_dedups_second_run():
    import app.alpaca_hf_notifier as nf
    import app.alpaca_hf_record as rec
    from datetime import datetime, timezone, timedelta
    await rec.set_last_seen(datetime.now(timezone.utc) - timedelta(hours=1))
    orders = [_order("QCOM", "buy", "buy_to_open", 12, 164.37, "q1")]
    with patch.object(nf, "get_orders_filled_range", return_value=orders), \
         patch.object(nf, "notify_hf_trade", new=AsyncMock()) as post:
        await nf.poll_and_notify()
        await nf.poll_and_notify()
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_close_triggers_investor_split():
    import app.alpaca_hf_notifier as nf
    import app.alpaca_hf_record as rec
    from datetime import datetime, timezone, timedelta
    await rec.set_last_seen(datetime.now(timezone.utc) - timedelta(hours=1))
    await rec.record_open("QCOM", "LONG", 12, 164.37, "t", "open1")
    orders = [_order("QCOM", "sell", "sell_to_close", 12, 164.84, "close1")]
    inv_result = [SimpleNamespace(name="Alice", portfolio_share=100.0)]
    breakdown = SimpleNamespace(investors=inv_result)
    with patch.object(nf, "get_orders_filled_range", return_value=orders), \
         patch.object(nf, "notify_hf_trade", new=AsyncMock()) as post, \
         patch.object(nf, "load_investors", return_value=["x"]), \
         patch.object(nf, "get_account", return_value=SimpleNamespace(equity="1000")), \
         patch.object(nf, "compute_breakdown", return_value=breakdown):
        await nf.poll_and_notify()
    msg = post.await_args.args[0]
    assert "Alice" in msg and "WIN" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_alpaca_hf_notifier.py -v`
Expected: FAIL (`_classify`/`poll_and_notify` undefined).

- [ ] **Step 3: Implement poller + recap**

Add imports and functions to `app/alpaca_hf_notifier.py`:

```python
import asyncio
import os
from datetime import timezone, timedelta

from app.trading.alpaca_client import get_orders_filled_range, get_account
from app.investors import load_investors, compute_breakdown
from app.notifications import notify_hf_trade, notify_hf_recap
from app import alpaca_hf_record as rec

_poll_lock = asyncio.Lock()

_INTENT_MAP = {
    "buy_to_open": ("LONG", "OPEN"),
    "sell_to_close": ("LONG", "CLOSE"),
    "sell_to_open": ("SHORT", "OPEN"),
    "buy_to_close": ("SHORT", "CLOSE"),
}


def _classify(order):
    pi = getattr(order, "position_intent", None)
    intent = getattr(pi, "value", pi)
    if intent in _INTENT_MAP:
        return _INTENT_MAP[intent]
    log.warning("Unclassifiable order %s (intent=%s)", getattr(order, "id", "?"), intent)
    return None


async def _investor_split(realized_pnl: float):
    try:
        investors = load_investors()
        if not investors:
            return []
        equity = float(get_account().equity)
        breakdown = compute_breakdown(investors, 0.0, equity)
        return [(r.name, realized_pnl * r.portfolio_share / 100.0)
                for r in breakdown.investors]
    except Exception as exc:
        log.warning("Investor split failed: %s", exc)
        return []


async def poll_and_notify() -> None:
    async with _poll_lock:
        now = datetime.now(timezone.utc)
        last = await rec.get_last_seen()
        if last is None:
            await rec.set_last_seen(now)  # seed; no backfill
            return
        after = last - timedelta(minutes=5)
        try:
            orders = get_orders_filled_range(after, now)
        except Exception as exc:
            log.warning("HF poll fetch failed: %s", exc)
            return

        newest = last
        for order in orders:
            oid = str(order.id)
            if order.symbol == "SPY":
                continue
            if await rec.is_seen(oid):
                continue
            classified = _classify(order)
            if classified is None:
                await rec.mark_seen(oid)
                continue
            direction, role = classified
            qty = float(order.filled_qty or 0)
            if qty <= 0:
                await rec.mark_seen(oid)
                continue
            price = float(order.filled_avg_price or 0)
            filled_at = order.filled_at or now
            ts_ct = filled_at.astimezone(CT)
            ts_iso = filled_at.isoformat()

            if role == "OPEN":
                await rec.record_open(order.symbol, direction, qty, price, ts_iso, oid)
                await rec.record_daily_fill({
                    "symbol": order.symbol, "role": "OPEN", "direction": direction,
                    "qty": qty, "price": price, "notional": price * qty, "ts": ts_iso,
                })
                await notify_hf_trade(format_open(order.symbol, direction, qty, price, ts_ct))
            else:
                result = await rec.record_close(order.symbol, direction, qty, price, ts_iso)
                split = await _investor_split(result.realized_pnl) if result.is_win is not None else []
                await rec.record_daily_fill({
                    "symbol": order.symbol, "role": "CLOSE", "direction": direction,
                    "qty": qty, "price": price, "realized_pnl": result.realized_pnl, "ts": ts_iso,
                })
                await notify_hf_trade(format_close(
                    order.symbol, direction, qty, price,
                    result.realized_pnl, result.pct, result.is_win, split, ts_ct,
                ))

            await rec.mark_seen(oid)
            if order.filled_at and order.filled_at > newest:
                newest = order.filled_at

        await rec.set_last_seen(min(newest, now))


def _day_label_ct() -> str:
    now = datetime.now(CT)
    return f"{now.strftime('%B')} {now.day}, {now.year}"


async def send_daily_recap() -> None:
    fills = await rec.pop_daily_fills()
    closes = [f for f in fills if f.get("role") == "CLOSE" and "realized_pnl" in f]
    wins = sum(1 for f in closes if f["realized_pnl"] > 0)
    losses = sum(1 for f in closes if f["realized_pnl"] <= 0)
    total_pnl = sum(f["realized_pnl"] for f in closes)
    await notify_hf_recap(format_recap(_day_label_ct(), fills, wins, losses, total_pnl))
```

Note: `compute_breakdown(investors, spy_price, real_total_equity)` — `spy_price` is unused for the split (pass `0.0`); only `portfolio_share` matters. `datetime` is already imported at the top of the module from Task 4.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_alpaca_hf_notifier.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/alpaca_hf_notifier.py tests/test_alpaca_hf_notifier.py
git commit -m "feat: add HF poller, classification, and daily recap"
```

---

### Task 6: Register scheduler jobs

**Files:**
- Modify: `app/scheduler.py` (imports + `setup_jobs:300`)
- Test: `tests/test_scheduler_hf.py` (create)

**Interfaces:**
- Consumes: `poll_and_notify`, `send_daily_recap` (Task 5).
- Produces: two registered jobs on the `scheduler` singleton with ids `hf_poll` and `hf_recap`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_hf.py
import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def test_hf_jobs_registered():
    from app import scheduler as sch
    sch.scheduler.remove_all_jobs()
    sch.setup_jobs()
    ids = {j.id for j in sch.scheduler.get_jobs()}
    assert "hf_poll" in ids
    assert "hf_recap" in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scheduler_hf.py -v`
Expected: FAIL (`hf_poll` not in ids).

- [ ] **Step 3: Register the jobs**

In `app/scheduler.py`, add imports at top:

```python
from apscheduler.triggers.interval import IntervalTrigger
from app.alpaca_hf_notifier import poll_and_notify as hf_poll_and_notify, send_daily_recap as hf_send_daily_recap
```

Add wrappers near the other job coroutines:

```python
async def _hf_poll() -> None:
    await hf_poll_and_notify()


async def _hf_recap() -> None:
    await hf_send_daily_recap()
```

Inside `setup_jobs()`, add:

```python
    scheduler.add_job(
        _profiled("hf_poll", _hf_poll),
        trigger=IntervalTrigger(minutes=2),
        id="hf_poll",
        max_instances=1,
        replace_existing=True,
    )
    scheduler.add_job(
        _profiled("hf_recap", _hf_recap),
        trigger=CronTrigger(hour=0, minute=0, timezone="America/Chicago"),
        id="hf_recap",
        max_instances=1,
        replace_existing=True,
    )
```

`CronTrigger` is already imported in `scheduler.py`. Confirm `_profiled(tag, fn)` returns the coroutine-callable APScheduler expects — match the exact call form used by the existing jobs registered in `setup_jobs()` (e.g. `_profiled("hf_poll", _hf_poll)` vs `_profiled(_hf_poll)`); if the existing jobs wrap differently, follow that form.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scheduler_hf.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_scheduler_hf.py
git commit -m "feat: register HF poll (2 min) and recap (00:00 CT) jobs"
```

---

### Task 7: Investor breakdown — non-SPY contribution line

**Files:**
- Modify: `app/investors.py` (`InvestorResult:244`, `compute_breakdown:265`)
- Modify: `app/pnl.py` (`send_investor_report:810`)
- Test: `tests/test_investors_nonspy.py` (create)

**Interfaces:**
- Consumes: `contribution_total` (`app/alpaca_hf_record.py`, Task 2).
- Produces: `InvestorResult.nonspy_contribution: float = 0.0`; `compute_breakdown(..., nonspy_pnl: float = 0.0)` populating it as `nonspy_pnl * portfolio_share / 100`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_investors_nonspy.py
import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def _make_investor(name, amount, entry_spy):
    # NOTE: verify Investor/Deposit field names in app/investors.py before running.
    from app.investors import Investor, Deposit
    return Investor(name=name, deposits=[Deposit(amount=amount, entry_spy=entry_spy, date="2026-01-01")], withdrawals=[])


def test_nonspy_contribution_split_by_share():
    from app.investors import compute_breakdown
    invs = [_make_investor("Alice", 5000, 100), _make_investor("Bob", 5000, 100)]
    b = compute_breakdown(invs, spy_price=100.0, real_total_equity=10000.0, nonspy_pnl=100.0)
    contribs = {r.name: r.nonspy_contribution for r in b.investors}
    assert round(contribs["Alice"], 2) == 50.0
    assert round(contribs["Bob"], 2) == 50.0


def test_nonspy_default_zero_preserves_behavior():
    from app.investors import compute_breakdown
    invs = [_make_investor("Alice", 5000, 100)]
    b = compute_breakdown(invs, spy_price=100.0, real_total_equity=5000.0)
    assert b.investors[0].nonspy_contribution == 0.0
```

(Adjust the `Investor`/`Deposit` constructor kwargs in `_make_investor` to match the real dataclasses in `app/investors.py` — check field names before running. If deposits need an `entry_spy`, the two equal deposits at the same SPY yield 50/50 shares.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_investors_nonspy.py -v`
Expected: FAIL (`nonspy_pnl` / `nonspy_contribution` unknown).

- [ ] **Step 3: Implement**

In `app/investors.py`, add to `InvestorResult`:

```python
    nonspy_contribution: float = 0.0
```

Change `compute_breakdown` signature and populate after `portfolio_share` is set:

```python
def compute_breakdown(
    investors: list[Investor],
    spy_price: float,
    real_total_equity: float,
    nonspy_pnl: float = 0.0,
) -> InvestorBreakdown:
    ...
    for r in results:
        r.portfolio_share = (r.current_equity / total_portfolio * 100) if total_portfolio else 0.0
        r.nonspy_contribution = nonspy_pnl * r.portfolio_share / 100.0
```

In `app/pnl.py` `send_investor_report()`, fetch and pass the total, and render a line:

```python
from app.alpaca_hf_record import contribution_total
...
nonspy_pnl = await contribution_total()
breakdown = compute_breakdown(investors, spy_price, real_total_equity, nonspy_pnl=nonspy_pnl)
# in the per-investor render loop, append:
#   f"Non-SPY contribution: {'+' if r.nonspy_contribution >= 0 else '-'}${abs(r.nonspy_contribution):,.2f}"
```

(Match the existing `send_investor_report` render structure — find where each `InvestorResult` is formatted and append the line there. The current call to `compute_breakdown` in that function only needs the `nonspy_pnl=` kwarg added.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_investors_nonspy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/investors.py app/pnl.py tests/test_investors_nonspy.py
git commit -m "feat: add non-SPY contribution line to investor breakdown"
```

---

### Task 8: End-to-end regression over 2026-08-27 sample fills

**Files:**
- Test: `tests/test_alpaca_hf_recap_sample.py` (create)

**Interfaces:**
- Consumes: everything above.
- Produces: an end-to-end aggregation test over the day's FILLED non-SPY orders.

- [ ] **Step 1: Write the test using the day's filled non-SPY orders**

Build a fixture of the FILLED non-SPY orders from 2026-08-27 (from the spec's
sample), classify each via `_classify`, feed through
`record_open`/`record_close`, collect `daily_fills`, then `format_recap`.
Assert the aggregate `wins`, `losses`, and `total_pnl` equal values computed
by hand from the round trips. Confirmed round trips from the sample (entry ->
exit, all filled):

```
QCOM  LONG  buy 164.37 -> sell 164.84   pnl = (164.84-164.37)*12 = +5.64  WIN
CSCO  SHORT sell 112.18 -> buy 112.2469  pnl = (112.18-112.2469)*17 = -1.14 LOSS
TSM   LONG  buy 425.47 -> sell 426.55   pnl = (426.55-425.47)*4  = +4.32  WIN
CRWD  SHORT sell 206.01 -> buy 205.76   pnl = (206.01-205.76)*9  = +2.25  WIN
MRNA  LONG  buy 144.29 -> sell(stop) 143.39  pnl = (143.39-144.29)*13 = -11.70 LOSS
TSLA  LONG  buy 352.9794 -> sell 353.05  pnl = (353.05-352.9794)*5 = +0.353 WIN
```

(Include only complete round trips where both legs FILLED that day; opens with
no matching close on the day — e.g. AMZN 256.80 open, SOFI 19.27 open, CSCO
111.95 open, QCOM 164.77 open — remain open lots and count as fills but not as
wins/losses. Use these exact numbers so the totals are deterministic; compute
the expected `wins`, `losses`, and summed `total_pnl` in the test.)

```python
# tests/test_alpaca_hf_recap_sample.py — skeleton
import os, pytest
from types import SimpleNamespace
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    import app.alpaca_hf_record as rec
    monkeypatch.setattr(rec, "_STATE_FILE", tmp_path / "hf.json")
    yield


@pytest.mark.asyncio
async def test_sample_day_recap_totals():
    import app.alpaca_hf_record as rec
    from app.alpaca_hf_notifier import format_recap
    trips = [  # (symbol, direction, qty, entry, exit)
        ("QCOM", "LONG", 12, 164.37, 164.84),
        ("CSCO", "SHORT", 17, 112.18, 112.2469),
        ("TSM", "LONG", 4, 425.47, 426.55),
        ("CRWD", "SHORT", 9, 206.01, 205.76),
        ("MRNA", "LONG", 13, 144.29, 143.39),
        ("TSLA", "LONG", 5, 352.9794, 353.05),
    ]
    for sym, d, q, entry, exit_ in trips:
        await rec.record_open(sym, d, q, entry, "t1", f"{sym}o")
        r = await rec.record_close(sym, d, q, exit_, "t2")
        await rec.record_daily_fill({"symbol": sym, "role": "OPEN", "direction": d,
                                     "qty": q, "price": entry, "notional": entry*q})
        await rec.record_daily_fill({"symbol": sym, "role": "CLOSE", "direction": d,
                                     "qty": q, "price": exit_, "realized_pnl": r.realized_pnl})
    fills = await rec.pop_daily_fills()
    closes = [f for f in fills if f["role"] == "CLOSE"]
    wins = sum(1 for f in closes if f["realized_pnl"] > 0)
    losses = sum(1 for f in closes if f["realized_pnl"] <= 0)
    total = sum(f["realized_pnl"] for f in closes)
    assert wins == 4
    assert losses == 2
    assert round(total, 2) == round(5.64 - 1.1373 + 4.32 + 2.25 - 11.70 + 0.353, 2)
    msg = format_recap("August 27, 2026", fills, wins, losses, total)
    assert "4 W" in msg and "2 L" in msg
```

- [ ] **Step 2: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass (new + existing).

- [ ] **Step 3: Commit**

```bash
git add tests/test_alpaca_hf_recap_sample.py
git commit -m "test: end-to-end HF recap over 2026-08-27 sample fills"
```

---

## Self-Review

- **Spec coverage:** live entry/exit notify (Tasks 3-5); win/loss + % + $ (Tasks 2,4); per-investor split (Tasks 5,7); daily recap all-fills + W/L/win-rate (Tasks 4-6); midnight CT (Task 6); equity breakdown edit (Task 7); two webhooks (Tasks 1,3); FILLED-only + SPY-excluded (Task 5); FIFO long/short (Task 2). All covered.
- **Placeholder scan:** none — every step has concrete code. The "verify constructor/render against live file" notes in Tasks 6-7 are integration checks, not deferred work.
- **Type consistency:** `CloseResult` fields (`matched_qty`, `realized_pnl`, `pct`, `is_win`, `unmatched_qty`) used identically in Tasks 2 and 5. `direction` strings `"LONG"`/`"SHORT"`, `role` `"OPEN"`/`"CLOSE"` consistent across Tasks 2/4/5/8. `nonspy_contribution` consistent across Task 7. Names `_STATE_FILE`, `contribution_total`, `notify_hf_trade`/`notify_hf_recap`, `format_open`/`format_close`/`format_recap` match their producers and consumers.

## Known implementation checks (call out, don't guess)

1. `get_http_client().post(...)` exact call shape — copy from the real `notify_trades` body (Task 3).
2. `_profiled` call form and whether it wraps a coroutine or a factory — match existing `setup_jobs()` registrations (Task 6).
3. Alpaca order attribute names: `filled_qty`, `filled_avg_price`, `position_intent.value`, `filled_at`, `id` — verify against alpaca-py `Order` (Task 5); tests stub them, so confirm on the first live run.
4. `Investor`/`Deposit` constructor field names for the Task 7 test fixture (Task 7).
5. `datetime.now(CT)` day formatting avoids `%-d` (not portable); `_day_label_ct` builds the label manually.
