import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Optional

import pytz

_ET = pytz.timezone("America/New_York")
# Trades before this date are from before the fund launched and must not
# pollute the Alpaca FIFO fallback stats.
_FUND_INCEPTION = _date(2026, 4, 27)

from app.early_access import load_spots

log = logging.getLogger(__name__)

_cache: dict = {"data": None, "expires": 0.0}
_write_lock = threading.Lock()

# Seed: shipped in the repo, read-only reference
_SEED_TRADES_PATH = Path(__file__).parent / "kimi_trades.json"
# Live: persistent disk on Render; accumulates new trades across deploys
_LIVE_TRADES_PATH = Path(os.getenv("DATA_DIR", "/data")) / "kimi_trades.json"


def init_live_trades() -> None:
    """Copy seed trades to persistent disk on first deploy (if live file absent)."""
    if _LIVE_TRADES_PATH.exists():
        return
    if not _SEED_TRADES_PATH.exists():
        return
    try:
        _LIVE_TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LIVE_TRADES_PATH.write_text(
            _SEED_TRADES_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        log.info("Seeded live kimi_trades.json from repo (%d bytes)", _LIVE_TRADES_PATH.stat().st_size)
    except Exception as exc:
        log.warning("Could not seed live kimi_trades.json: %s", exc)


def _load_manual_trades() -> list | None:
    """Prefer the live persistent-disk file; fall back to the repo seed."""
    for path in (_LIVE_TRADES_PATH, _SEED_TRADES_PATH):
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to load %s: %s", path, exc)
    return None


def append_kimi_trade(ticker: str, buy: float, sell: float, qty: float) -> None:
    """Append a completed SPY round trip to the live kimi_trades.json and bust cache.

    Called automatically by trade_notifier on every SPY sell fill so the
    public chart stays current without any manual intervention.
    """
    if ticker.upper() != "SPY":
        return
    pct_pnl  = round((sell - buy) / buy * 100, 2)
    dollar_pnl = round((sell - buy) * qty, 2)
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    entry = {
        "date":       datetime.strptime(today, "%Y-%m-%d").strftime("%m/%d"),
        "full_date":  today,
        "buy":        round(buy, 2),
        "sell":       round(sell, 2),
        "pct_pnl":   pct_pnl,
        "dollar_pnl": dollar_pnl,
        "won":        dollar_pnl > 0,
    }
    with _write_lock:
        trades = _load_manual_trades() or []
        trades.append(entry)
        _LIVE_TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LIVE_TRADES_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(trades, indent=2), encoding="utf-8")
        tmp.replace(_LIVE_TRADES_PATH)
    _cache["expires"] = 0.0  # bust cache so next request reads the updated file
    log.info(
        "Auto-appended SPY trade: buy=%.2f sell=%.2f pct=%.2f%% (%s)",
        buy, sell, pct_pnl, "WIN" if dollar_pnl >= 0 else "LOSS",
    )


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
                    "won":       dollar_pnl > 0,
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
        # Strip orders before fund inception — same contamination guard as the original FIFO bug fix
        _inception_dt = datetime.combine(_FUND_INCEPTION, datetime.min.time()).replace(tzinfo=timezone.utc)
        orders = [o for o in orders if o.filled_at and o.filled_at >= _inception_dt]
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
