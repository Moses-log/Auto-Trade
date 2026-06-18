"""
rh_deposit_log.py — Tracks external cash deposits to the Robinhood trading account.

Separate from investors.json (SPY fund) — this records direct bank deposits
into the RH account used by the Claude Manager.  Used by claude_callouts.py
to strip deposit inflation from the Portfolio Manager track record chart.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TypedDict

log = logging.getLogger(__name__)

_LOG_FILE = Path(os.getenv("RH_DEPOSIT_LOG_PATH", "/data/rh_deposits.json"))


class RhDeposit(TypedDict):
    date: str     # YYYY-MM-DD
    amount: float


def load_rh_deposits() -> list[RhDeposit]:
    if not _LOG_FILE.exists():
        return []
    try:
        return json.loads(_LOG_FILE.read_text())
    except Exception as exc:
        log.warning("rh_deposit_log: failed to read %s: %s", _LOG_FILE, exc)
        return []


def append_rh_deposit(date: str, amount: float) -> None:
    deposits = load_rh_deposits()
    deposits.append({"date": date, "amount": amount})
    deposits.sort(key=lambda d: d["date"])
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(deposits, indent=2))
    tmp.replace(_LOG_FILE)


def clear_rh_deposits() -> int:
    """Delete all logged deposits. Returns the count that were removed."""
    deposits = load_rh_deposits()
    count = len(deposits)
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps([], indent=2))
    tmp.replace(_LOG_FILE)
    return count


def get_rh_deposit_events() -> list[tuple[str, float]]:
    """Return [(iso_date, amount), ...] for all logged RH deposits, sorted ascending."""
    return [(d["date"], d["amount"]) for d in load_rh_deposits()]
