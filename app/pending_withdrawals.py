from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_FILE = Path(os.getenv("PENDING_WITHDRAWALS_PATH", "/data/pending_withdrawals.json"))
_lock = threading.Lock()


def _load() -> list:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text()).get("pending", [])
        except Exception:
            return []
    return []


def _save(records: list) -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pending": records}))
    tmp.replace(_FILE)  # atomic rename — survives kill signals mid-write


def save_pending_withdrawal(
    withdrawal_id: str,
    investor: str,
    amount: float,
    requested_at: str,
    run_at: str,
) -> None:
    with _lock:
        records = _load()
        records.append({
            "id": withdrawal_id,
            "investor": investor,
            "amount": amount,
            "requested_at": requested_at,
            "run_at": run_at,
        })
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
