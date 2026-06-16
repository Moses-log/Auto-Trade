"""
idempotency.py — Duplicate-alert protection, backed by persistent disk.

TradingView can fire the same alert multiple times (network retries, bar
replays). We track recently-processed alert keys in a disk-backed JSON file
so the store survives Render restarts and redeploys.

The TTL window is controlled by IDEMPOTENCY_TTL (default 300 s / 5 min).
The file path is controlled by IDEMPOTENCY_PATH (default idempotency.json).
Set IDEMPOTENCY_PATH=/data/idempotency.json when using Render persistent disk.
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from app.config import settings
from app.models import AlertPayload

_FILE = Path(os.getenv("IDEMPOTENCY_PATH", "idempotency.json"))
_lock = threading.Lock()


def _make_key(payload: AlertPayload) -> str:
    if payload.order_id:
        raw = f"{payload.ticker}:{payload.order_id}"
    elif payload.timestamp:
        raw = f"{payload.ticker}:{payload.action}:{payload.timestamp}"
    else:
        # No reliable unique field — use contracts+price to differentiate
        # distinct alerts while still blocking identical TradingView retries
        raw = f"{payload.ticker}:{payload.action}:{payload.contracts}:{payload.price}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _load() -> dict:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(seen: dict) -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen))
    tmp.replace(_FILE)


def _evict_expired(seen: dict) -> dict:
    now = time.time()
    return {k: exp for k, exp in seen.items() if exp > now}


def is_duplicate(payload: AlertPayload) -> bool:
    """Return True if this alert was already processed within the TTL window."""
    with _lock:
        seen = _evict_expired(_load())
        _save(seen)  # persist eviction so expired entries don't accumulate
        return _make_key(payload) in seen


def mark_processed(payload: AlertPayload) -> None:
    """Record an alert as processed so future duplicates are rejected."""
    with _lock:
        seen = _evict_expired(_load())
        seen[_make_key(payload)] = time.time() + settings.idempotency_ttl
        _save(seen)
