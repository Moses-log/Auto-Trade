from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime
from typing import Optional

import pytz

from app.notifications import notify_trades
from app.trading.alpaca_client import get_next_trading_day, get_order, get_position
from app.trade_record import format_record, record_trade_result

log = logging.getLogger(__name__)
CT = pytz.timezone("America/Chicago")
_ET = pytz.timezone("America/New_York")

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
    record_str: Optional[str] = None,
) -> str:
    is_buy = action.upper().split(" (")[0] in _BUY_ACTIONS
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
            result_label = "🟢 WIN"
        else:
            pnl_str = f"-${abs(dollar_pnl):,.2f}"
            pct_str = f"{pct_pnl:.2f}%"
            result_label = "🔴 LOSS"
        lines.append(f"P&L: {pnl_str} ({pct_str}) {result_label}")

    lines.append(f"🕐 {time_str}")

    if record_str:
        lines.append("")
        lines.append(f"Record: {record_str}")

    return "\n".join(lines)


def _format_queued_message(ticker: str, action: str, alert_price: Optional[float]) -> str:
    now = datetime.now(CT)
    hour = int(now.strftime("%I"))
    tz_label = now.strftime("%Z")
    time_str = f"{hour}:{now.strftime('%M %p')} {tz_label} — {now.strftime('%B')} {now.day}, {now.year}"
    price_str = f"≈${alert_price:,.2f}" if alert_price else "unknown price"
    return "\n".join([
        f"⏳ **{action.upper()} — {ticker}**",
        f"Order queued for next market open @ {price_str}",
        f"🕐 {time_str}",
    ])


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
        pending_order_id: Optional[str] = None

        orders = result.get("orders", [])
        if orders:
            order_id = orders[0].get("alpaca_order_id")
            if order_id:
                order = get_order(order_id)
                if order and order.filled_avg_price:
                    filled_price = float(order.filled_avg_price)
                    filled_qty = float(order.filled_qty) if order.filled_qty else None
                elif order:
                    pending_order_id = order_id

        if pending_order_id:
            await notify_trades(_format_queued_message(ticker, action, alert_price))
            _schedule_pending_followup(pending_order_id, ticker, action, alert_price, avg_entry_price)
            return

        position_qty = 0.0
        pos = get_position(ticker)
        if pos and pos.qty:
            position_qty = float(pos.qty)

        dollar_pnl: Optional[float] = None
        pct_pnl: Optional[float] = None
        if avg_entry_price and filled_price and filled_qty and avg_entry_price != 0:
            dollar_pnl = (filled_price - avg_entry_price) * filled_qty
            pct_pnl = (filled_price - avg_entry_price) / avg_entry_price * 100

        record_str: Optional[str] = None
        if dollar_pnl is not None:
            wins, losses = await record_trade_result(dollar_pnl >= 0)
            record_str = format_record(wins, losses)

        message = _format_trade_message(
            ticker=ticker,
            action=action,
            filled_price=filled_price,
            alert_price=alert_price,
            filled_qty=filled_qty,
            position_qty=position_qty,
            dollar_pnl=dollar_pnl,
            pct_pnl=pct_pnl,
            record_str=record_str,
        )
        await notify_trades(message)

    except Exception as exc:
        log.warning("Trade notification failed: %s", exc)


async def notify_pending_order_fill(
    order_id: str,
    ticker: str,
    action: str,
    alert_price: Optional[float],
    avg_entry_price: Optional[float],
) -> None:
    """Called at next market open to resolve fill details for a queued order."""
    filled_price: Optional[float] = None
    filled_qty: Optional[float] = None

    for attempt in range(12):
        try:
            order = get_order(order_id)
            if order and order.filled_avg_price and order.filled_qty:
                filled_price = float(order.filled_avg_price)
                filled_qty = float(order.filled_qty)
                break
        except Exception as exc:
            log.warning("Error polling order %s (attempt %d): %s", order_id, attempt, exc)
        if attempt < 11:
            await asyncio.sleep(10)

    if filled_qty is None:
        log.warning("Queued order %s still unfilled after 2 minutes at market open", order_id)
        return

    pos = get_position(ticker)
    position_qty = float(pos.qty) if pos and pos.qty else 0.0

    dollar_pnl: Optional[float] = None
    pct_pnl: Optional[float] = None
    if avg_entry_price and filled_price and filled_qty and avg_entry_price != 0:
        dollar_pnl = (filled_price - avg_entry_price) * filled_qty
        pct_pnl = (filled_price - avg_entry_price) / avg_entry_price * 100

    record_str: Optional[str] = None
    if dollar_pnl is not None:
        wins, losses = await record_trade_result(dollar_pnl >= 0)
        record_str = format_record(wins, losses)

    message = _format_trade_message(
        ticker=ticker,
        action=f"{action} (filled at open)",
        filled_price=filled_price,
        alert_price=alert_price,
        filled_qty=filled_qty,
        position_qty=position_qty,
        dollar_pnl=dollar_pnl,
        pct_pnl=pct_pnl,
        record_str=record_str,
    )
    await notify_trades(message)


def _schedule_pending_followup(
    order_id: str,
    ticker: str,
    action: str,
    alert_price: Optional[float],
    avg_entry_price: Optional[float],
) -> None:
    from app.scheduler import scheduler

    next_day = get_next_trading_day()
    run_dt = _ET.localize(datetime.combine(next_day, dtime(9, 31)))

    scheduler.add_job(
        notify_pending_order_fill,
        "date",
        run_date=run_dt,
        args=[order_id, ticker, action, alert_price, avg_entry_price],
        id=f"pending_{order_id}",
        replace_existing=True,
    )
    log.info("Queued order %s scheduled for resolution at %s", order_id, run_dt)
