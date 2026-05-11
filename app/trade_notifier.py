from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz

from app.notifications import notify_trades
from app.trading.alpaca_client import get_order, get_position

log = logging.getLogger(__name__)
CT = pytz.timezone("America/Chicago")

_BUY_ACTIONS = {"BUY", "BASE_ENTRY", "ADD_LEVERAGE"}


def _format_trade_message(
    ticker: str,
    action: str,
    filled_price: Optional[float],
    alert_price: Optional[float],
    filled_qty: Optional[float],
    position_qty: float,
    dollar_pnl: Optional[float],
    pct_pnl: Optional[float],
) -> str:
    is_buy = action.upper() in _BUY_ACTIONS
    emoji = "🟢" if is_buy else "🔴"

    if filled_price is not None:
        price_str = f"${filled_price:,.2f}"
    elif alert_price is not None:
        price_str = f"≈${alert_price:,.2f}"
    else:
        price_str = "unknown"

    qty_str = f"{filled_qty:g}" if filled_qty is not None else "?"

    now = datetime.now(CT)
    hour = int(now.strftime("%I"))
    tz_label = now.strftime("%Z")
    time_str = f"{hour}:{now.strftime('%M %p')} {tz_label} — {now.strftime('%B')} {now.day}, {now.year}"

    lines = [
        f"{emoji} **{action.upper()} — {ticker}**",
        f"Qty: {qty_str} shares @ {price_str}",
        f"Position: {position_qty:g} shares",
    ]

    if dollar_pnl is not None and pct_pnl is not None:
        if dollar_pnl >= 0:
            pnl_str = f"+${dollar_pnl:,.2f}"
            pct_str = f"+{pct_pnl:.2f}%"
            pnl_emoji = "🟢"
        else:
            pnl_str = f"-${abs(dollar_pnl):,.2f}"
            pct_str = f"{pct_pnl:.2f}%"
            pnl_emoji = "🔴"
        lines.append(f"P&L: {pnl_str} ({pct_str}) {pnl_emoji}")

    lines.append(f"🕐 {time_str}")
    return "\n".join(lines)


async def notify_trade(
    ticker: str,
    action: str,
    result: dict,
    alert_price: Optional[float],
    avg_entry_price: Optional[float],
) -> None:
    try:
        filled_price: Optional[float] = None
        filled_qty: Optional[float] = None

        orders = result.get("orders", [])
        if orders:
            order_id = orders[0].get("alpaca_order_id")
            if order_id:
                order = get_order(order_id)
                if order and order.filled_avg_price:
                    filled_price = float(order.filled_avg_price)
                    filled_qty = float(order.filled_qty) if order.filled_qty else None

        position_qty = 0.0
        pos = get_position(ticker)
        if pos and pos.qty:
            position_qty = float(pos.qty)

        dollar_pnl: Optional[float] = None
        pct_pnl: Optional[float] = None
        if avg_entry_price and filled_price and filled_qty and avg_entry_price != 0:
            dollar_pnl = (filled_price - avg_entry_price) * filled_qty
            pct_pnl = (filled_price - avg_entry_price) / avg_entry_price * 100

        message = _format_trade_message(
            ticker=ticker,
            action=action,
            filled_price=filled_price,
            alert_price=alert_price,
            filled_qty=filled_qty,
            position_qty=position_qty,
            dollar_pnl=dollar_pnl,
            pct_pnl=pct_pnl,
        )
        await notify_trades(message)

    except Exception as exc:
        log.warning("Trade notification failed: %s", exc)
