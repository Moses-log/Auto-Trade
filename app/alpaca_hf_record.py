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
