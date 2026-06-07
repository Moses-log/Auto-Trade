"""
tax.py — Realized gain/loss computation for IRS tax reporting.

Alpaca: FIFO lot matching over the full year's order history.
Robinhood: trade record JSON — all trades treated as short-term
           because the Kimi strategy is algorithmic (< 1 yr holds).

Note: Alpaca fetches up to 500 orders per 2-year window. If you executed
more than 500 orders in that window, download the official 1099-B from
Alpaca for a complete picture.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ── Robinhood ─────────────────────────────────────────────────────────────────

def compute_rh_tax_summary(year: int) -> dict:
    """Compute RH realized gains from rh_trade_record.json for the given year."""
    from app.rh_trade_record import get_all_trades

    gains = 0.0
    losses = 0.0
    win_count = 0
    loss_count = 0

    for t in get_all_trades():
        try:
            ts = datetime.fromisoformat(t["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.year != year:
                continue
            pnl = float(t.get("dollar_pnl", 0.0))
            if pnl >= 0:
                gains += pnl
                win_count += 1
            else:
                losses += pnl
                loss_count += 1
        except Exception:
            pass

    return {
        "short_term_gains": gains,
        "short_term_losses": losses,
        "short_term_net": gains + losses,
        "win_count": win_count,
        "loss_count": loss_count,
        "total_trades": win_count + loss_count,
    }


# ── Alpaca FIFO ───────────────────────────────────────────────────────────────

def _fifo_match(orders: list, tax_year: int) -> list:
    """FIFO lot matching — returns realized gain events for sells in tax_year.

    Processes all orders (including prior-year) to maintain correct lot state,
    but only records gains/losses for sells whose fill date falls in tax_year.
    """
    filled = sorted(
        [o for o in orders if o.filled_at is not None],
        key=lambda o: o.filled_at,
    )

    buy_lots: dict = defaultdict(deque)
    realized = []

    for order in filled:
        symbol = order.symbol
        qty = float(order.filled_qty or 0)
        price = float(order.filled_avg_price or 0)
        fill_dt = order.filled_at
        is_buy = "buy" in str(order.side).lower()

        if is_buy:
            buy_lots[symbol].append({"qty": qty, "price": price, "date": fill_dt})
            continue

        # Sell: consume lots (always, to keep FIFO state correct)
        qty_remaining = qty
        while qty_remaining > 0 and buy_lots[symbol]:
            lot = buy_lots[symbol][0]
            consumed = min(lot["qty"], qty_remaining)

            if fill_dt.year == tax_year:
                holding_days = (fill_dt - lot["date"]).days
                realized.append({
                    "symbol": symbol,
                    "qty": consumed,
                    "proceeds": price * consumed,
                    "cost": lot["price"] * consumed,
                    "gain": (price - lot["price"]) * consumed,
                    "short_term": holding_days < 365,
                    "sell_date": fill_dt,
                })

            lot["qty"] -= consumed
            qty_remaining -= consumed
            if lot["qty"] <= 0:
                buy_lots[symbol].popleft()

        # Any remaining qty has no matched buy lot in our fetch window
        if qty_remaining > 0 and fill_dt.year == tax_year:
            realized.append({
                "symbol": symbol,
                "qty": qty_remaining,
                "proceeds": price * qty_remaining,
                "cost": None,
                "gain": None,
                "short_term": True,
                "sell_date": fill_dt,
                "unknown_basis": True,
            })

    return realized


async def compute_alpaca_tax_summary(year: int) -> dict:
    """Fetch Alpaca order history and compute FIFO realized gains for tax_year."""
    import asyncio
    from datetime import timezone as _tz
    from app.trading.alpaca_client import get_orders_filled_range

    loop = asyncio.get_running_loop()
    # Fetch from start of year-1 so positions opened in the prior year
    # have matching buy lots for FIFO matching.
    after = datetime(year - 1, 1, 1, tzinfo=_tz.utc)
    until = datetime(year + 1, 1, 1, tzinfo=_tz.utc)

    try:
        orders = await loop.run_in_executor(None, get_orders_filled_range, after, until)
    except Exception as exc:
        log.error("Failed to fetch Alpaca orders for %d tax summary: %s", year, exc)
        return {"error": str(exc)}

    events = _fifo_match(orders, year)
    known = [e for e in events if not e.get("unknown_basis")]
    unknown = [e for e in events if e.get("unknown_basis")]

    st_gains  = sum(e["gain"] for e in known if e["short_term"] and e["gain"] > 0)
    st_losses = sum(e["gain"] for e in known if e["short_term"] and e["gain"] < 0)
    lt_gains  = sum(e["gain"] for e in known if not e["short_term"] and e["gain"] > 0)
    lt_losses = sum(e["gain"] for e in known if not e["short_term"] and e["gain"] < 0)

    return {
        "short_term_gains":       st_gains,
        "short_term_losses":      st_losses,
        "short_term_net":         st_gains + st_losses,
        "long_term_gains":        lt_gains,
        "long_term_losses":       lt_losses,
        "long_term_net":          lt_gains + lt_losses,
        "unknown_basis_proceeds": sum(e["proceeds"] for e in unknown),
        "unknown_basis_count":    len(unknown),
        "sell_event_count":       len(events),
    }
