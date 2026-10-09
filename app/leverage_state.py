"""
leverage_state.py — Tracks the Alpaca fill price of ADD_LEVERAGE orders per ticker.

Alpaca's position.avg_entry_price is a blended average across all shares
(base + leverage). Using it as the cost basis for REMOVE_LEVERAGE produces
wrong P&L when the base position was opened at a different price — the blended
average can sit below the leverage exit price even when the leverage trade lost.

Storing the actual ADD_LEVERAGE fill price here gives REMOVE_LEVERAGE the
correct per-trade cost basis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_STATE_FILE = Path(os.getenv("LEVERAGE_STATE_PATH", "leverage_entry.json"))
_REPO_FILE  = Path("leverage_entry.json")
_lock       = asyncio.Lock()


def load_leverage_entry(ticker: str) -> Optional[float]:
    """Return the stored ADD_LEVERAGE fill price for *ticker*, or None."""
    for path in (_STATE_FILE, _REPO_FILE):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                val = data.get(ticker)
                return float(val) if val is not None else None
            except Exception:
                pass
    return None


# Open leverage share counts live in the same file under this key, so the
# nightly Gist backup of leverage_entry.json covers them too.
_QTY_KEY = "_open_qty"


def _read_state() -> dict:
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _write_state(data: dict) -> None:
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(_STATE_FILE)


def load_leverage_qty(ticker: str) -> Optional[float]:
    """Return the shares ADD_LEVERAGE has bought and REMOVE_LEVERAGE has not
    yet sold for *ticker*, or None if nothing is recorded."""
    qty = (_read_state().get(_QTY_KEY) or {}).get(ticker)
    return float(qty) if qty else None


def add_leverage_qty(ticker: str, qty: float) -> float:
    """Add *qty* shares to the open leverage count for *ticker*; return the total."""
    data = _read_state()
    open_qty = data.setdefault(_QTY_KEY, {})
    total = round(float(open_qty.get(ticker) or 0.0) + qty, 6)
    open_qty[ticker] = total
    _write_state(data)
    log.info("Saved open leverage qty", extra={"ticker": ticker, "added": qty, "open_qty": total})
    return total


def clear_leverage_qty(ticker: str) -> None:
    """Forget the open leverage count for *ticker* (leverage fully closed)."""
    data = _read_state()
    if (data.get(_QTY_KEY) or {}).pop(ticker, None) is not None:
        _write_state(data)
        log.info("Cleared open leverage qty", extra={"ticker": ticker})


async def save_leverage_entry(ticker: str, price: float) -> None:
    """Persist the ADD_LEVERAGE fill price for *ticker*."""
    async with _lock:
        data: dict = {}
        if _STATE_FILE.exists():
            try:
                data = json.loads(_STATE_FILE.read_text(encoding="utf-8-sig"))
            except Exception:
                pass
        data[ticker] = price
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_STATE_FILE)
        log.info("Saved leverage entry price", extra={"ticker": ticker, "price": price})
