import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.early_access import load_spots

log = logging.getLogger(__name__)

_cache: dict = {"data": None, "expires": 0.0}

_MANUAL_TRADES_PATH = Path(__file__).parent / "kimi_trades.json"


def _load_manual_trades() -> list | None:
    if not _MANUAL_TRADES_PATH.exists():
        return None
    try:
        return json.loads(_MANUAL_TRADES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to load manual trades: %s", exc)
        return None


def _fmt_full_date(s: str) -> str:
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime(f"%b {dt.day}, %Y")
    except Exception:
        return s


def compute_stats_from_manual(trades_data: list) -> dict:
    """Build the same stats dict as compute_stats(), but from kimi_trades.json."""
    if not trades_data:
        return {
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "profit_factor": 0,
            "date_range": {"from": "", "to": ""},
            "cumulative_returns": [],
        }

    wins   = [t for t in trades_data if t["won"]]
    losses = [t for t in trades_data if not t["won"]]
    gross_win  = sum(t["dollar_pnl"] for t in wins)
    gross_loss = abs(sum(t["dollar_pnl"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 0

    cumulative: list = []
    running = 0.0
    for i, t in enumerate(trades_data, 1):
        running += t["pct_pnl"]
        cumulative.append({
            "trade":      i,
            "pct":        round(running, 4),
            "won":        t["won"],
            "trade_pct":  round(t["pct_pnl"], 2),
            "date":       t["date"],
            "buy":        t["buy"],
            "sell":       t["sell"],
        })

    if len(cumulative) > 100:
        cumulative = cumulative[-100:]

    return {
        "trades":        len(trades_data),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / len(trades_data) * 100, 1),
        "profit_factor": profit_factor,
        "date_range": {
            "from": _fmt_full_date(trades_data[0].get("full_date", "")),
            "to":   _fmt_full_date(trades_data[-1].get("full_date", "")),
        },
        "cumulative_returns": cumulative,
    }


def compute_stats(filled_orders: list) -> dict:
    """LIFO-match buys to sells and return performance stats dict."""
    orders = sorted(filled_orders, key=lambda o: o.filled_at)

    buy_stack: deque = deque()  # FIFO: [qty_remaining, fill_price, fill_dt]
    trades:    list = []
    first_dt:  Optional[object] = None
    last_sell_dt: Optional[object] = None

    for o in orders:
        side  = str(o.side)
        qty   = float(o.filled_qty)
        price = float(o.filled_avg_price)
        dt    = o.filled_at.astimezone(timezone.utc)

        if first_dt is None:
            first_dt = dt

        if "BUY" in side.upper():
            buy_stack.append([qty, price, dt])
        else:
            remaining  = qty
            cost_basis = 0.0
            matched    = 0.0

            while remaining > 1e-6 and buy_stack:
                bq, bp, _ = buy_stack[0]
                take        = min(remaining, bq)
                cost_basis += take * bp
                matched    += take
                remaining  -= take
                buy_stack[0][0] -= take
                if buy_stack[0][0] < 1e-6:
                    buy_stack.popleft()

            if matched > 1e-6:
                proceeds   = matched * price
                dollar_pnl = proceeds - cost_basis
                pct_pnl    = (dollar_pnl / cost_basis) * 100
                trades.append({
                    "won":       dollar_pnl >= 0,
                    "dollar_pnl": dollar_pnl,
                    "pct_pnl":   pct_pnl,
                    "date":      dt.strftime("%m/%d"),
                    "buy_price": round(cost_basis / matched, 2),
                    "sell_price": round(price, 2),
                })
                last_sell_dt = dt

    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "profit_factor": 0,
            "date_range": {"from": "", "to": ""},
            "cumulative_returns": [],
        }

    wins   = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    gross_win  = sum(t["dollar_pnl"] for t in wins)
    gross_loss = abs(sum(t["dollar_pnl"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 0

    cumulative: list = []
    running = 0.0
    for i, t in enumerate(trades, 1):
        running += t["pct_pnl"]
        cumulative.append({
            "trade":      i,
            "pct":        round(running, 4),
            "won":        t["won"],
            "trade_pct":  round(t["pct_pnl"], 2),
            "date":       t["date"],
            "buy":        t["buy_price"],
            "sell":       t["sell_price"],
        })

    def _fmt(dt) -> str:
        if dt is None:
            return ""
        day = dt.strftime("%d").lstrip("0") or "0"
        return dt.strftime(f"%b {day}, %Y")

    # Stats reflect all trades; display capped at 100 most recent
    if len(cumulative) > 100:
        cumulative = cumulative[-100:]

    return {
        "trades":       len(trades),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(len(wins) / len(trades) * 100, 1),
        "profit_factor": profit_factor,
        "date_range":   {"from": _fmt(first_dt), "to": _fmt(last_sell_dt)},
        "cumulative_returns": cumulative,
    }


async def get_public_stats() -> dict:
    """Return cached stats.

    Prefers kimi_trades.json (manually verified data) when present.
    Falls back to live Alpaca order computation if the file is absent.
    """
    now = time.time()
    if _cache["data"] is not None and now < _cache["expires"]:
        return _cache["data"]

    manual = _load_manual_trades()
    if manual is not None:
        stats = compute_stats_from_manual(manual)
        stats["spots_remaining"] = load_spots()
        _cache["data"]    = stats
        _cache["expires"] = now + 3600
        return stats

    loop = asyncio.get_running_loop()
    try:
        from app.trading.alpaca_client import get_all_spy_orders
        orders = await loop.run_in_executor(None, get_all_spy_orders)
    except Exception as exc:
        log.warning("Failed to fetch SPY orders for public stats: %s", exc)
        orders = []

    stats = compute_stats(orders)
    stats["spots_remaining"] = load_spots()

    if orders:
        _cache["data"]    = stats
        _cache["expires"] = now + 3600
    elif _cache["data"] is not None:
        stale = dict(_cache["data"])
        stale["spots_remaining"] = stats["spots_remaining"]
        return stale
    else:
        _cache["data"]    = stats
        _cache["expires"] = now + 60

    return stats
