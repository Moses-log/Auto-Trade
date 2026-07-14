"""risk_guardrails.py — Code-enforced position/sector risk limits.

Hard-clamps any single position to <= MAX_POSITION_PCT and computes post-trade
sector exposure for alerting (no auto-scale). Pure except the yfinance sector
adapter. claude_manager / pnl imports are lazy (inside functions) to avoid a
cycle, since claude_manager imports this module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_POSITION_PCT = 25.0
MAX_SECTOR_PCT = 50.0
_CLAMPED_ACTIONS = ("BUY", "DOUBLE_DOWN", "TRIM")


@dataclass
class ClampEvent:
    ticker: str
    original_pct: float
    clamped_pct: float   # always MAX_POSITION_PCT


def clamp_position_weights(trades: list[dict]) -> list[ClampEvent]:
    """Clamp target_weight_pct to <= MAX_POSITION_PCT for BUY/DOUBLE_DOWN/TRIM,
    in place. Returns one ClampEvent per trade actually clamped."""
    events: list[ClampEvent] = []
    for t in trades:
        if t.get("action") not in _CLAMPED_ACTIONS:
            continue
        wt = t.get("target_weight_pct")
        if wt is None:
            continue
        if wt > MAX_POSITION_PCT:
            events.append(ClampEvent((t.get("ticker") or "?").upper(), float(wt), MAX_POSITION_PCT))
            t["target_weight_pct"] = MAX_POSITION_PCT
    return events
