from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_RECORD_FILE = Path(os.getenv("TRADE_RECORD_PATH", "trade_record.json"))
_lock = asyncio.Lock()


def _load() -> dict:
    if _RECORD_FILE.exists():
        try:
            return json.loads(_RECORD_FILE.read_text())
        except Exception:
            pass
    return {"wins": 0, "losses": 0}


def _save(record: dict) -> None:
    _RECORD_FILE.write_text(json.dumps(record))


async def record_trade_result(is_win: bool) -> tuple[int, int]:
    """Increment win or loss, persist to disk, return (wins, losses)."""
    async with _lock:
        record = _load()
        if is_win:
            record["wins"] += 1
        else:
            record["losses"] += 1
        _save(record)

    return record["wins"], record["losses"]


def format_record(wins: int, losses: int) -> str:
    total = wins + losses
    if total == 0:
        return "0-0"
    return f"{wins}-{losses} ({wins / total * 100:.0f}% Win Rate)"