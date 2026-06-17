"""
rh_keep_alive_state.py — persists the last robinhood_keep_alive run time.

robinhood_keep_alive is checked daily via a cron trigger (wall-clock
anchored, immune to restart drift) but should only actually run every
~3 days. If a daily check is missed (app down at the scheduled time), the
next day's check sees >=3 days elapsed since the last run and catches up
immediately — no permanent starvation, no extra logins beyond the
intended cadence.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_RECORD_FILE = Path(os.getenv("RH_KEEP_ALIVE_STATE_PATH", "/data/rh_keep_alive_state.json"))


def get_last_run_ts() -> Optional[int]:
    """Unix timestamp of the last keep-alive run, or None if it has never run."""
    if not _RECORD_FILE.exists():
        return None
    try:
        return json.loads(_RECORD_FILE.read_text())["last_run_ts"]
    except Exception:
        log.warning("rh_keep_alive_state: failed to read %s", _RECORD_FILE, exc_info=True)
        return None


def record_run(ts: int) -> None:
    """Persist the unix timestamp of a keep-alive run."""
    _RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RECORD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_run_ts": ts}))
    tmp.replace(_RECORD_FILE)
