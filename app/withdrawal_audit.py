from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_FILE = Path(os.getenv("WITHDRAWAL_AUDIT_PATH", "/data/withdrawal_audit.json"))
_lock = threading.Lock()


def _load_json() -> list:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text()).get("audit", [])
        except Exception:
            return []
    return []


def _save_json(records: list) -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"audit": records}))
    tmp.replace(_FILE)


_CORE_FIELDS = ("id", "investor", "amount", "requested_at", "run_at", "status")


def _append_sqlite(entry: dict) -> None:
    from app import db
    core = {k: entry[k] for k in _CORE_FIELDS}
    extra = {k: v for k, v in entry.items() if k not in _CORE_FIELDS}
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO withdrawal_audit(withdrawal_id, investor, amount, requested_at, "
            "run_at, status, extra_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (core["id"], core["investor"], core["amount"], core["requested_at"],
             core["run_at"], core["status"],
             json.dumps(extra) if extra else None,
             datetime.now(timezone.utc).isoformat()))


def _load_sqlite() -> list:
    from app import db
    rows = db.get_conn().execute(
        "SELECT withdrawal_id, investor, amount, requested_at, run_at, status, extra_json "
        "FROM withdrawal_audit ORDER BY id").fetchall()
    result = []
    for r in rows:
        entry = {
            "id": r["withdrawal_id"], "investor": r["investor"], "amount": r["amount"],
            "requested_at": r["requested_at"], "run_at": r["run_at"], "status": r["status"],
        }
        if r["extra_json"]:
            entry.update(json.loads(r["extra_json"]))
        result.append(entry)
    return result


def _export_json() -> None:
    _save_json(_load_sqlite())


def append_withdrawal_audit(
    withdrawal_id: str,
    investor: str,
    amount: float,
    requested_at: str,
    run_at: str,
    status: str,
    **extra,
) -> None:
    entry = {
        "id": withdrawal_id,
        "investor": investor,
        "amount": amount,
        "requested_at": requested_at,
        "run_at": run_at,
        "status": status,
    }
    entry.update(extra)
    if settings.use_sqlite:
        _append_sqlite(entry)
    else:
        with _lock:
            records = _load_json()
            records.append(entry)
            _save_json(records)
    log.info("Recorded withdrawal audit entry %s: %s", withdrawal_id, status)


def load_withdrawal_audit() -> list:
    return _load_sqlite() if settings.use_sqlite else _load_json()


# Regenerate withdrawal_audit.json after every committed SQLite write batch.
try:
    from app import db as _db
    _db.register_exporter(_export_json)
except Exception:  # pragma: no cover
    pass
