"""
rh_keep_alive_state.py — persists the Robinhood keep-alive schedule.

After each refresh a next_run_ts is drawn randomly: 1 or 2 days out,
at a random time between 1:00 AM and 4:59 AM ET. The cron job checks
every 15 minutes inside that window and fires as soon as now >= next_run_ts.
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

log = logging.getLogger(__name__)

_RECORD_FILE = Path(os.getenv("RH_KEEP_ALIVE_STATE_PATH", "/data/rh_keep_alive_state.json"))
_ET = pytz.timezone("America/New_York")


def _pick_next_run_ts(now_ts: int) -> int:
    """Pick a random ET timestamp 1–2 days out, between 1:00 and 4:59 AM."""
    now = datetime.fromtimestamp(now_ts, _ET)
    day_offset = random.randint(1, 2)
    target_date = (now + timedelta(days=day_offset)).date()
    hour   = random.randint(1, 4)
    minute = random.randint(0, 59)
    target = _ET.localize(datetime(target_date.year, target_date.month, target_date.day, hour, minute))
    return int(target.timestamp())


def get_next_run_ts() -> Optional[int]:
    """Unix timestamp when the next keep-alive should fire, or None if never recorded."""
    if not _RECORD_FILE.exists():
        return None
    try:
        return json.loads(_RECORD_FILE.read_text()).get("next_run_ts")
    except Exception:
        log.warning("rh_keep_alive_state: failed to read %s", _RECORD_FILE, exc_info=True)
        return None


def get_last_run_ts() -> Optional[int]:
    """Unix timestamp of the last keep-alive run, or None if it has never run."""
    if not _RECORD_FILE.exists():
        return None
    try:
        return json.loads(_RECORD_FILE.read_text()).get("last_run_ts")
    except Exception:
        log.warning("rh_keep_alive_state: failed to read %s", _RECORD_FILE, exc_info=True)
        return None


def record_run(ts: int) -> None:
    """Persist the last run timestamp and schedule the next one at a random 1–5 AM ET slot."""
    next_ts = _pick_next_run_ts(ts)
    next_dt = datetime.fromtimestamp(next_ts, _ET).strftime("%Y-%m-%d %I:%M %p %Z")
    _RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RECORD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_run_ts": ts, "next_run_ts": next_ts}))
    tmp.replace(_RECORD_FILE)
    log.info("rh_keep_alive: next refresh scheduled for %s", next_dt)
