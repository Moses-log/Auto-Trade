import asyncio
import logging
import time
from datetime import timezone
from typing import Optional

from app.early_access import load_spots

log = logging.getLogger(__name__)

_cache: dict = {"data": None, "expires": 0.0}


def compute_stats(filled_orders: list) -> dict:
    """LIFO-match buys to sells and return performance stats dict."""
    orders = sorted(filled_orders, key=lambda o: o.filled_at)

    buy_stack: list = []   # each entry: [qty_remaining, fill_price, fill_dt]
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
                bq, bp, _ = buy_stack[-1]
                take        = min(remaining, bq)
                cost_basis += take * bp
                matched    += take
                remaining  -= take
                buy_stack[-1][0] -= take
                if buy_stack[-1][0] < 1e-6:
                    buy_stack.pop()

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
    """Return cached stats, refreshing from Alpaca if the 1-hour TTL has expired."""
    now = time.time()
    if _cache["data"] is not None and now < _cache["expires"]:
        return _cache["data"]

    loop = asyncio.get_running_loop()
    try:
        from app.trading.alpaca_client import get_all_spy_orders
        orders = await loop.run_in_executor(None, get_all_spy_orders)
    except Exception as exc:
        log.warning("Failed to fetch SPY orders for public stats: %s", exc)
        orders = []

    stats = compute_stats(orders)
    stats["spots_remaining"] = load_spots()

    _cache["data"]    = stats
    _cache["expires"] = now + 3600
    return stats
