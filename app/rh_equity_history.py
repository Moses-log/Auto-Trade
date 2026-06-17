"""
rh_equity_history.py — Daily RH-equity / SPY-price snapshot store.

The robin_stocks `get_historical_portfolio` endpoint (RH equity_historicals)
was retired by Robinhood (404 on /portfolios/historicals/{account}/) and is
gone for good. Instead, once per day at the same scheduler tick that sends
Alpaca's reports, we record RH's current equity and SPY's current price
*together* as one paired snapshot. Period comparisons (daily/weekly/etc.)
are then computed from pairs of these snapshots — both numbers always come
from the same point in time, so they can't drift apart the way separately
date-ranged Alpaca/yfinance fetches did.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, TypedDict

log = logging.getLogger(__name__)

_RECORD_FILE = Path(os.getenv("RH_EQUITY_HISTORY_PATH", "/data/rh_equity_history.json"))
_lock = asyncio.Lock()


class EquitySnapshot(TypedDict):
    date: str        # ET calendar date, "YYYY-MM-DD"
    ts: int           # unix timestamp of the snapshot
    equity: float     # RH portfolio equity
    spy_close: float  # SPY price at the same moment


def _load() -> List[EquitySnapshot]:
    if not _RECORD_FILE.exists():
        return []
    try:
        return json.loads(_RECORD_FILE.read_text())
    except Exception:
        log.warning("rh_equity_history: failed to read %s", _RECORD_FILE, exc_info=True)
        return []


def _save(snapshots: List[EquitySnapshot]) -> None:
    _RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RECORD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshots))
    tmp.replace(_RECORD_FILE)


async def record_snapshot(date: str, ts: int, equity: float, spy_close: float) -> None:
    """Upsert today's RH-equity/SPY-price pair, keyed by ET calendar date."""
    async with _lock:
        snapshots = [s for s in _load() if s["date"] != date]
        snapshots.append({"date": date, "ts": ts, "equity": equity, "spy_close": spy_close})
        snapshots.sort(key=lambda s: s["ts"])
        _save(snapshots)


def get_snapshots() -> List[EquitySnapshot]:
    """All recorded snapshots, oldest first."""
    return sorted(_load(), key=lambda s: s["ts"])
