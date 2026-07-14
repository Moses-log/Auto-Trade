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


def compute_sector_exposure(positions, trades, portfolio_value, sector_of) -> dict:
    """Post-trade percent-of-portfolio by sector. None-sector tickers bucket into 'Unknown'."""
    if not portfolio_value or portfolio_value <= 0:
        return {}
    trade_by_ticker = {t["ticker"].upper(): t for t in trades if t.get("ticker")}
    weights: dict[str, float] = {}
    held: set[str] = set()
    for p in positions:
        sym = p["symbol"].upper()
        held.add(sym)
        t = trade_by_ticker.get(sym)
        action = t.get("action") if t else None
        if action == "SELL":
            weights[sym] = 0.0
        elif action in ("BUY", "DOUBLE_DOWN", "TRIM"):
            weights[sym] = float(t.get("target_weight_pct") or 0.0)
        else:  # HOLD or no matching trade
            weights[sym] = (p.get("qty", 0) * p.get("current_price", 0)) / portfolio_value * 100
    for tk, t in trade_by_ticker.items():
        if tk not in held and t.get("action") in ("BUY", "DOUBLE_DOWN"):
            weights[tk] = float(t.get("target_weight_pct") or 0.0)
    exposure: dict[str, float] = {}
    for tk, w in weights.items():
        if w <= 0:
            continue
        sector = sector_of(tk) or "Unknown"
        exposure[sector] = exposure.get(sector, 0.0) + w
    return exposure


def sector_warnings(exposure: dict) -> list:
    """Sectors (excluding 'Unknown') strictly above MAX_SECTOR_PCT, highest first."""
    over = [(s, p) for s, p in exposure.items() if s != "Unknown" and p > MAX_SECTOR_PCT]
    over.sort(key=lambda x: x[1], reverse=True)
    return [f"{s} {p:.0f}% (> {MAX_SECTOR_PCT:.0f}% cap)" for s, p in over]
