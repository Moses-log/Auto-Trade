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

from app.config import settings

log = logging.getLogger(__name__)

_LOG_FILE = Path(os.getenv("RH_DEPOSIT_LOG_PATH", "/data/rh_deposits.json"))


class RhDeposit(TypedDict):
    date: str     # YYYY-MM-DD
    amount: float


def _load_json() -> list[RhDeposit]:
    if not _LOG_FILE.exists():
        return []
    try:
        return json.loads(_LOG_FILE.read_text())
    except Exception as exc:
        log.warning("rh_deposit_log: failed to read %s: %s", _LOG_FILE, exc)
        return []


def _write_json(deposits: list[RhDeposit]) -> None:
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(deposits, indent=2))
    tmp.replace(_LOG_FILE)


def _load_sqlite() -> list[RhDeposit]:
    from app import db
    rows = db.get_conn().execute(
        "SELECT date, amount FROM rh_deposits ORDER BY date, id").fetchall()
    return [{"date": r["date"], "amount": r["amount"]} for r in rows]


def _write_sqlite(deposits: list[RhDeposit]) -> None:
    from app import db
    with db.writer() as conn:
        conn.execute("DELETE FROM rh_deposits")
        for d in deposits:
            conn.execute("INSERT INTO rh_deposits(date, amount) VALUES(?,?)",
                         (d["date"], d["amount"]))


def _export_json() -> None:
    _write_json(_load_sqlite())


def load_rh_deposits() -> list[RhDeposit]:
    return _load_sqlite() if settings.use_sqlite else _load_json()


def append_rh_deposit(date: str, amount: float) -> None:
    deposits = load_rh_deposits()
    deposits.append({"date": date, "amount": amount})
    deposits.sort(key=lambda d: d["date"])
    if settings.use_sqlite:
        _write_sqlite(deposits)
    else:
        _write_json(deposits)


def clear_rh_deposits() -> int:
    """Delete all logged deposits. Returns the count that were removed."""
    count = len(load_rh_deposits())
    if settings.use_sqlite:
        _write_sqlite([])
    else:
        _write_json([])
    return count


def get_rh_deposit_events() -> list[tuple[str, float]]:
    """Return [(iso_date, amount), ...] for all logged RH deposits, sorted ascending."""
    return [(d["date"], d["amount"]) for d in load_rh_deposits()]


try:
    from app import db as _db
    _db.register_exporter(_export_json)
except Exception:  # pragma: no cover
    pass
