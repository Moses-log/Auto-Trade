from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_FILE = Path(os.getenv("CLAUDE_PORTFOLIO_PATH", "/data/claude_portfolio.json"))
_lock = threading.Lock()


def _load() -> dict:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text())
        except Exception:
            pass
    return {"positions": [], "closed": [], "wins": 0, "losses": 0}


def _save(data: dict) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_FILE)


def get_position(ticker: str) -> Optional[dict]:
    data = _load()
    ticker = ticker.upper()
    return next((p for p in data["positions"] if p["ticker"] == ticker), None)


def open_position(
    ticker: str,
    qty: float,
    entry_price: float,
    tweet_url: Optional[str] = None,
) -> None:
    ticker = ticker.upper()
    with _lock:
        data = _load()
        for pos in data["positions"]:
            if pos["ticker"] == ticker:
                # Average into existing position
                total_qty = pos["qty"] + qty
                pos["entry_price"] = (
                    pos["entry_price"] * pos["qty"] + entry_price * qty
                ) / total_qty
                pos["qty"] = total_qty
                if tweet_url:
                    pos["tweet_url"] = tweet_url
                _save(data)
                log.info("Claude portfolio: added to %s position", ticker)
                return
        data["positions"].append({
            "ticker": ticker,
            "qty": qty,
            "entry_price": entry_price,
            "entry_date": date.today().isoformat(),
            "tweet_url": tweet_url or "",
        })
        _save(data)
    log.info("Claude portfolio: opened %s @ %.2f", ticker, entry_price)


def close_position(
    ticker: str,
    exit_price: float,
    tweet_url: Optional[str] = None,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (qty, dollar_pnl, pct_pnl). All None if no open position."""
    ticker = ticker.upper()
    with _lock:
        data = _load()
        pos = next((p for p in data["positions"] if p["ticker"] == ticker), None)
        if pos is None:
            return None, None, None

        qty = pos["qty"]
        entry_price = pos["entry_price"]
        dollar_pnl = (exit_price - entry_price) * qty
        pct_pnl = (exit_price - entry_price) / entry_price * 100 if entry_price else 0.0

        data["positions"] = [p for p in data["positions"] if p["ticker"] != ticker]
        data["closed"].append({
            "ticker": ticker,
            "qty": qty,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "dollar_pnl": dollar_pnl,
            "pct_pnl": pct_pnl,
            "entry_date": pos.get("entry_date", ""),
            "exit_date": date.today().isoformat(),
            "tweet_url": tweet_url or pos.get("tweet_url", ""),
        })
        if dollar_pnl >= 0:
            data["wins"] = data.get("wins", 0) + 1
        else:
            data["losses"] = data.get("losses", 0) + 1
        _save(data)

    log.info("Claude portfolio: closed %s @ %.2f, P&L=%.2f", ticker, exit_price, dollar_pnl)
    return qty, dollar_pnl, pct_pnl


def trim_position(
    ticker: str,
    qty_sold: float,
    exit_price: float,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Partially reduce a position. Returns (qty_sold, dollar_pnl, pct_pnl). All None if no position."""
    ticker = ticker.upper()
    with _lock:
        data = _load()
        pos = next((p for p in data["positions"] if p["ticker"] == ticker), None)
        if pos is None:
            return None, None, None

        qty_sold = min(qty_sold, pos["qty"])
        entry_price = pos["entry_price"]
        dollar_pnl = (exit_price - entry_price) * qty_sold
        pct_pnl = (exit_price - entry_price) / entry_price * 100 if entry_price else 0.0

        pos["qty"] -= qty_sold
        if pos["qty"] < 0.0001:
            data["positions"] = [p for p in data["positions"] if p["ticker"] != ticker]
        data["closed"].append({
            "ticker": ticker,
            "qty": qty_sold,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "dollar_pnl": dollar_pnl,
            "pct_pnl": pct_pnl,
            "entry_date": pos.get("entry_date", ""),
            "exit_date": date.today().isoformat(),
            "partial": True,
        })
        if dollar_pnl >= 0:
            data["wins"] = data.get("wins", 0) + 1
        else:
            data["losses"] = data.get("losses", 0) + 1
        _save(data)

    log.info("Claude portfolio: trimmed %s by %.4f shares @ %.2f, P&L=%.2f", ticker, qty_sold, exit_price, dollar_pnl)
    return qty_sold, dollar_pnl, pct_pnl


def get_record() -> tuple[int, int]:
    """Returns (wins, losses)."""
    data = _load()
    return data.get("wins", 0), data.get("losses", 0)
