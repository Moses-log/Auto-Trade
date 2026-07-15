from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

_FILE = Path(os.getenv("PENDING_WITHDRAWALS_PATH", "/data/pending_withdrawals.json"))
_lock = threading.Lock()


def _load_json() -> list:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text()).get("pending", [])
        except Exception:
            return []
    return []


def _save_json(records: list) -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pending": records}))
    tmp.replace(_FILE)  # atomic rename — survives kill signals mid-write


def _load_sqlite() -> list:
    from app import db
    rows = db.get_conn().execute(
        "SELECT id, investor, amount, requested_at, run_at, spy_price "
        "FROM pending_withdrawals ORDER BY rowid").fetchall()
    records = []
    for r in rows:
        rec = {
            "id": r["id"], "investor": r["investor"], "amount": r["amount"],
            "requested_at": r["requested_at"], "run_at": r["run_at"],
        }
        if r["spy_price"] is not None:
            rec["spy_price"] = r["spy_price"]
        records.append(rec)
    return records


def _save_sqlite(records: list) -> None:
    from app import db
    with db.writer() as conn:
        conn.execute("DELETE FROM pending_withdrawals")
        for rec in records:
            conn.execute(
                "INSERT INTO pending_withdrawals(id, investor, amount, requested_at, run_at, spy_price) "
                "VALUES(?,?,?,?,?,?)",
                (rec["id"], rec["investor"], rec["amount"], rec["requested_at"],
                 rec["run_at"], rec.get("spy_price")))


def _export_json() -> None:
    _save_json(_load_sqlite())


def _load() -> list:
    return _load_sqlite() if settings.use_sqlite else _load_json()


def _save(records: list) -> None:
    if settings.use_sqlite:
        _save_sqlite(records)
    else:
        _save_json(records)


def save_pending_withdrawal(
    withdrawal_id: str,
    investor: str,
    amount: float,
    requested_at: str,
    run_at: str,
    spy_price: Optional[float] = None,
) -> None:
    with _lock:
        records = _load()
        record: dict = {
            "id": withdrawal_id,
            "investor": investor,
            "amount": amount,
            "requested_at": requested_at,
            "run_at": run_at,
        }
        if spy_price is not None:
            record["spy_price"] = spy_price
        records.append(record)
        _save(records)
    log.info("Saved pending withdrawal %s for %s ($%.2f)", withdrawal_id, investor, amount)


def remove_pending_withdrawal(withdrawal_id: str) -> None:
    with _lock:
        records = _load()
        records = [r for r in records if r["id"] != withdrawal_id]
        _save(records)
    log.info("Removed pending withdrawal %s from disk", withdrawal_id)


def load_pending_withdrawals() -> list:
    return _load()


def get_pending_withdrawal(withdrawal_id: str) -> Optional[dict]:
    for record in _load():
        if record["id"] == withdrawal_id:
            return record
    return None


# Regenerate pending_withdrawals.json after every committed SQLite write batch.
try:
    from app import db as _db
    _db.register_exporter(_export_json)
except Exception:  # pragma: no cover
    pass
