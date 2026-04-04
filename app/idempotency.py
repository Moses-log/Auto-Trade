"""
idempotency.py — Duplicate-alert protection.

TradingView can fire the same alert multiple times (network retries, bar
replays). We track recently-processed alert IDs in an in-memory TTL store.

The TTL window is controlled by IDEMPOTENCY_TTL (default 300 s / 5 min).

For multi-process / multi-instance deployments swap this out for a Redis
SET with EXPIRE — the interface is identical, only the backend changes.
"""

import hashlib
import time
from typing import Dict, Tuple

from app.config import settings
from app.models import AlertPayload

# {alert_key: expiry_unix_timestamp}
_seen: Dict[str, float] = {}


def _make_key(payload: AlertPayload) -> str:
    """
    Build a deduplication key.

    Priority:
      1. payload.order_id  — unique per strategy order (best)
      2. hash(ticker + action + timestamp) — fallback when order_id is absent
    """
    if payload.order_id:
        raw = f"{payload.ticker}:{payload.order_id}"
    else:
        raw = f"{payload.ticker}:{payload.action}:{payload.timestamp}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _evict_expired() -> None:
    """Remove stale entries so the dict doesn't grow unboundedly."""
    now = time.monotonic()
    expired = [k for k, exp in _seen.items() if exp < now]
    for k in expired:
        del _seen[k]


def is_duplicate(payload: AlertPayload) -> bool:
    """Return True if this alert was already processed within the TTL window."""
    _evict_expired()
    key = _make_key(payload)
    return key in _seen


def mark_processed(payload: AlertPayload) -> None:
    """Record an alert as processed so future duplicates are rejected."""
    _evict_expired()
    key = _make_key(payload)
    _seen[key] = time.monotonic() + settings.idempotency_ttl
