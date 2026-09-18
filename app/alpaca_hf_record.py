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


async def record_close(symbol, direction, qty, exit_price, ts, shares=None) -> CloseResult:
    async with _lock:
        state = _load()
        lots = list(state["open_lots"].get(symbol, []))
        remaining = float(qty)
        matched = 0.0
        pnl = 0.0
        cost = 0.0
        # Match newest matching lot first (LIFO). The external HF strategy opens
        # and closes each position in the same-second bracket, so a close belongs
        # to the lot it *just* opened, not a stale older lot of the same symbol.
        # FIFO here booked a close against an unrelated week-old lot -> a phantom
        # loss (the PLTR bug). Iterate newest->oldest; keep every untouched lot
        # and any leftover in its original position so order is preserved.
        for i in range(len(lots) - 1, -1, -1):
            if remaining <= 0:
                break
            lot = lots[i]
            if lot["direction"] != direction:
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
            lots[i] = {**lot, "qty": leftover} if leftover > 1e-9 else None
        state["open_lots"][symbol] = [lot for lot in lots if lot is not None]

        if matched <= 0:
            _save(state)
            return CloseResult(0.0, 0.0, 0.0, None, remaining)

        pct = (pnl / cost * 100) if cost else 0.0
        is_win = pnl > 0
        trade = {
            "symbol": symbol, "direction": direction, "qty": matched,
            "exit_price": exit_price, "realized_pnl": round(pnl, 4),
            "pct": round(pct, 4), "is_win": is_win, "closed_ts": ts,
        }
        # Freeze each investor's dollar share of THIS trade at close time, so
        # the cumulative per-investor contribution stays exact even when fund
        # shares later change (deposits/withdrawals/new investors).
        if shares:
            trade["investor_split"] = {
                name: round(pnl * pct_share / 100.0, 4) for name, pct_share in shares
            }
        state["closed_trades"].append(trade)
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


async def contribution_by_investor(fallback_shares=None) -> dict:
    """Cumulative non-SPY realized P&L attributed per investor.

    Each closed trade carries the dollar split frozen at its close time
    (`investor_split`), so shifting fund shares never retro-distort earlier
    trades. Legacy trades recorded before splits were stored fall back to
    `fallback_shares` (a list of (name, pct)) — current shares — so their
    P&L is still attributed rather than dropped.
    """
    async with _lock:
        trades = _load().get("closed_trades", [])
    totals: dict = {}
    for t in trades:
        split = t.get("investor_split")
        if split:
            for name, amt in split.items():
                totals[name] = totals.get(name, 0.0) + amt
        elif fallback_shares:
            pnl = t.get("realized_pnl", 0.0)
            for name, pct_share in fallback_shares:
                totals[name] = totals.get(name, 0.0) + pnl * pct_share / 100.0
    return totals


async def realized_pnl_today(tz: str = "America/Chicago") -> float:
    """Sum realized P&L of non-SPY round trips closed today (in the given tz).

    `closed_ts` is stored as the fill's tz-aware ISO timestamp. Trades whose
    timestamp can't be parsed (e.g. legacy/test rows) are skipped.
    """
    import pytz

    zone = pytz.timezone(tz)
    today = datetime.now(zone).date()
    async with _lock:
        trades = _load().get("closed_trades", [])
    total = 0.0
    for t in trades:
        raw = t.get("closed_ts")
        try:
            when = datetime.fromisoformat(raw).astimezone(zone).date()
        except (TypeError, ValueError):
            continue
        if when == today:
            total += t.get("realized_pnl", 0.0)
    return total
