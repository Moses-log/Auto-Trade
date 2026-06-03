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
        _STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
        log.info("Saved leverage entry price", extra={"ticker": ticker, "price": price})
