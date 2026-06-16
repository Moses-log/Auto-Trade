from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List

log = logging.getLogger(__name__)

_RECORD_FILE = Path(os.getenv("RH_TRADE_RECORD_PATH", "/data/rh_trade_record.json"))
_lock = asyncio.Lock()


def _load() -> dict:
    if _RECORD_FILE.exists():
        try:
            data = json.loads(_RECORD_FILE.read_text())
            data.setdefault("trades", [])  # backward compat with records that predate trade history
            return data
        except Exception:
            pass
    return {"wins": 0, "losses": 0, "trades": []}


def _save(record: dict) -> None:
    _RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RECORD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(_RECORD_FILE)


async def record_rh_trade(
    is_win: bool,
    ticker: str,
    dollar_pnl: float,
) -> tuple[int, int]:
    """Record a closed RH trade with timestamp. Returns (wins, losses)."""
    async with _lock:
        record = _load()
        if is_win:
            record["wins"] += 1
        else:
            record["losses"] += 1
        record["trades"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "dollar_pnl": round(dollar_pnl, 4),
            "is_win": is_win,
        })
        _save(record)
    return record["wins"], record["losses"]


def get_all_trades() -> List[dict]:
    """Return all stored trade entries."""
    return _load().get("trades", [])


def get_totals() -> tuple[int, int]:
    """Return (wins, losses) all-time."""
    record = _load()
    return record.get("wins", 0), record.get("losses", 0)


def format_rh_record(wins: int, losses: int) -> str:
    total = wins + losses
    if total == 0:
        return "0-0"
    return f"{wins}-{losses} ({wins / total * 100:.0f}% Win Rate)"
