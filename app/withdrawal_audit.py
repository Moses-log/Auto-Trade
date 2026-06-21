from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_FILE = Path(os.getenv("WITHDRAWAL_AUDIT_PATH", "/data/withdrawal_audit.json"))
_lock = threading.Lock()


def _load() -> list:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text()).get("audit", [])
        except Exception:
            return []
    return []


def _save(records: list) -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"audit": records}))
    tmp.replace(_FILE)


def append_withdrawal_audit(
    withdrawal_id: str,
    investor: str,
    amount: float,
    requested_at: str,
    run_at: str,
    status: str,
    **extra,
) -> None:
    with _lock:
        records = _load()
        entry = {
            "id": withdrawal_id,
            "investor": investor,
            "amount": amount,
            "requested_at": requested_at,
            "run_at": run_at,
            "status": status,
        }
        entry.update(extra)
        records.append(entry)
        _save(records)
    log.info("Recorded withdrawal audit entry %s: %s", withdrawal_id, status)


def load_withdrawal_audit() -> list:
    return _load()
