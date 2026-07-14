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


def resolve_sectors(enriched, trades, fetch_sector):
    """Build {ticker: sector|None} from enriched holdings, filling any BUY/DOUBLE_DOWN
    candidate not already known via fetch_sector. Returns (sector_map, unknown_tickers)."""
    sector_map: dict = {}
    for h in enriched:
        tk = (h.get("ticker") or "").upper()
        if tk:
            sector_map[tk] = h.get("sector")
    unknown: list = []
    for t in trades:
        if t.get("action") not in ("BUY", "DOUBLE_DOWN"):
            continue
        tk = (t.get("ticker") or "").upper()
        if not tk:
            continue
        if sector_map.get(tk) is None:
            sec = fetch_sector(tk)
            sector_map[tk] = sec
            if sec is None and tk not in unknown:
                unknown.append(tk)
    return sector_map, unknown


def _yf_sector_fetch(ticker: str):
    """Bounded yfinance sector lookup for a single ticker, or None on failure."""
    try:
        import yfinance as yf
        from app.pnl import _yf_fetch
        info = _yf_fetch(lambda: yf.Ticker(ticker).info)
        return info.get("sector") if info else None
    except Exception as exc:
        log.warning("risk_guardrails sector fetch failed for %s: %s", ticker, exc)
        return None


def format_guardrail_embed(clamps, warnings, unknown_tickers):
    """Combined ⚠️ RISK GUARDRAIL embed, or None when nothing fired."""
    if not clamps and not warnings:
        return None
    from app.claude_manager import _embed, _field, _CLR_ORANGE, _timestamp
    fields = []
    if clamps:
        lines = [f"{c.ticker}: {c.original_pct:.0f}% → clamped to {c.clamped_pct:.0f}%" for c in clamps]
        fields.append(_field("Position cap (25%)", "\n".join(lines), inline=False))
    if warnings:
        note = "\n".join(warnings)
        if unknown_tickers:
            note += f"\n(sector unresolved: {', '.join(unknown_tickers)})"
        fields.append(_field("Sector concentration (> 50%)", note, inline=False))
    return _embed("⚠️ RISK GUARDRAIL", _CLR_ORANGE, fields=fields, footer=_timestamp())
