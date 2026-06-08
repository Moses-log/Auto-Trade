from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime
from typing import Optional

import pytz

from app.notifications import notify_robinhood, notify_rh_session
from app.rh_trade_record import format_rh_record, record_rh_trade

log = logging.getLogger(__name__)
CT = pytz.timezone("America/Chicago")
_ET = pytz.timezone("America/New_York")

_BUY_ACTIONS = {"BUY", "BASE_ENTRY", "ADD_LEVERAGE", "REVERSE_TO_LONG"}


def _format_rh_queued_message(
    ticker: str,
    action: str,
    qty: Optional[float],
    price_est: Optional[float],
) -> str:
    now = datetime.now(CT)
    hour = int(now.strftime("%I"))
    tz_label = now.strftime("%Z")
    time_str = f"{hour}:{now.strftime('%M %p')} {tz_label} — {now.strftime('%B')} {now.day}, {now.year}"
    price_str = f"≈${price_est:,.2f}" if price_est else "unknown price"
    qty_str = f"{qty:g}" if qty is not None else "?"
    return "\n".join([
        f"⏳ **RH {action.upper()} — {ticker}**",
        f"Qty: {qty_str} shares queued for next market open @ {price_str}",
        f"🕐 {time_str}",
    ])


def _format_rh_message(
    ticker: str,
    action: str,
    fill_price: Optional[float],
    alert_price: Optional[float],
    qty: Optional[float],
    position_qty: float,
    dollar_pnl: Optional[float],
    pct_pnl: Optional[float],
    record_str: Optional[str] = None,
) -> str:
    is_buy = action.upper().split(" (")[0] in _BUY_ACTIONS
    emoji = "🟢" if is_buy else "🔴"

    if fill_price is not None:
        price_str = f"${fill_price:,.2f}"
    elif alert_price is not None:
        price_str = f"≈${alert_price:,.2f}"
    else:
        price_str = "unknown"

    qty_str = f"{qty:g}" if qty is not None else "?"

    now = datetime.now(CT)
    hour = int(now.strftime("%I"))
    tz_label = now.strftime("%Z")
    time_str = f"{hour}:{now.strftime('%M %p')} {tz_label} — {now.strftime('%B')} {now.day}, {now.year}"

    lines = [
        f"{emoji} **RH {action.upper()} — {ticker}**",
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
            pct_str = f"-{abs(pct_pnl):.2f}%"
            result_label = "🔴 LOSS"
        lines.append(f"P&L: {pnl_str} ({pct_str}) {result_label}")

    lines.append(f"🕐 {time_str}")

    if record_str:
        lines.append("")
        lines.append(f"RH Record: {record_str}")

    return "\n".join(lines)


async def notify_rh_trade(
    ticker: str,
    action: str,
    rh_result: dict,
    alert_price: Optional[float],
) -> None:
    """Send a formatted RH trade notification. Never raises."""
    try:
        status = rh_result.get("status")

        if status == "failed":
            reason = rh_result.get("reason", "unknown")
            await notify_robinhood(f"❌ RH {action.upper()} {ticker} FAILED: {reason}")
            if reason == "session expired":
                await notify_rh_session(
                    "⚠️ **ROBINHOOD SESSION EXPIRED**\n"
                    "Trade failed — session expired mid-run.\n"
                    "POST `/robinhood-auth` to re-authenticate."
                )
            return

        if status == "skipped":
            if rh_result.get("reason") == "session unavailable":
                await notify_robinhood(
                    f"⚠️ RH {action.upper()} {ticker} skipped — session unavailable"
                )
            return

        if status != "ok":
            return

        # Order accepted but markets are closed — queued for next open
        if rh_result.get("queued"):
            qty = rh_result.get("qty")
            message = _format_rh_queued_message(
                ticker=ticker,
                action=action,
                qty=qty,
                price_est=rh_result.get("price_est") or alert_price,
            )
            await notify_robinhood(message)
            order_id = rh_result.get("order_id")
            if order_id:
                await _schedule_rh_pending_followup(
                    order_id=order_id,
                    ticker=ticker,
                    action=action,
                    side=rh_result.get("side", "buy"),
                    qty=qty,
                    alert_price=alert_price,
                    avg_buy_price=rh_result.get("avg_buy_price"),
                )
            return

        # "no position to close" or "short not supported" — no P&L to show
        if rh_result.get("note"):
            note = rh_result["note"]
            closed_long = rh_result.get("closed_long")
            if closed_long is True:
                note += " ✅ Long position closed."
            elif closed_long is False:
                note += " ℹ️ No long position found."
            await notify_robinhood(f"ℹ️ RH {action.upper()} {ticker}: {note}")
            return

        side = rh_result.get("side", "buy")
        qty: Optional[float] = rh_result.get("qty")
        fill_price: Optional[float] = rh_result.get("fill_price")
        avg_buy_price: Optional[float] = rh_result.get("avg_buy_price")
        position_qty: float = rh_result.get("position_qty", 0.0)

        dollar_pnl: Optional[float] = None
        pct_pnl: Optional[float] = None
        record_str: Optional[str] = None

        if side == "sell" and avg_buy_price and fill_price and qty and avg_buy_price != 0:
            dollar_pnl = (fill_price - avg_buy_price) * qty
            pct_pnl = (fill_price - avg_buy_price) / avg_buy_price * 100
            wins, losses = await record_rh_trade(dollar_pnl >= 0, ticker, dollar_pnl)
            record_str = format_rh_record(wins, losses)

        message = _format_rh_message(
            ticker=ticker,
            action=action,
            fill_price=fill_price,
            alert_price=alert_price,
            qty=qty,
            position_qty=position_qty,
            dollar_pnl=dollar_pnl,
            pct_pnl=pct_pnl,
            record_str=record_str,
        )
        await notify_robinhood(message)

    except Exception as exc:
        log.warning("RH trade notification failed: %s", exc)


async def notify_rh_pending_fill(
    order_id: str,
    ticker: str,
    action: str,
    side: str,
    qty: Optional[float],
    alert_price: Optional[float],
    avg_buy_price: Optional[float],
) -> None:
    """Called at next market open to resolve fill details for a queued RH order."""
    import robin_stocks.robinhood as r

    fill_price: Optional[float] = None
    loop = asyncio.get_running_loop()

    for attempt in range(12):
        try:
            info = await loop.run_in_executor(None, r.get_stock_order_info, order_id)
            if info and info.get("state") == "filled" and info.get("average_price"):
                fill_price = float(info["average_price"])
                break
        except Exception as exc:
            log.warning("Error polling RH order %s (attempt %d): %s", order_id, attempt, exc)
        if attempt < 11:
            await asyncio.sleep(10)

    from app.pending_orders import remove_pending_order
    remove_pending_order(order_id)

    if fill_price is None:
        log.warning("RH queued order %s still unfilled after 2 minutes at market open", order_id)
        await notify_robinhood(
            f"⚠️ RH queued order for {ticker} ({action.upper()}) did not fill at open — check Robinhood"
        )
        return

    dollar_pnl: Optional[float] = None
    pct_pnl: Optional[float] = None
    record_str: Optional[str] = None

    if side == "sell" and avg_buy_price and qty and avg_buy_price != 0:
        dollar_pnl = (fill_price - avg_buy_price) * qty
        pct_pnl = (fill_price - avg_buy_price) / avg_buy_price * 100
        wins, losses = await record_rh_trade(dollar_pnl >= 0, ticker, dollar_pnl)
        record_str = format_rh_record(wins, losses)

    position_qty = qty if side == "buy" else 0.0
    message = _format_rh_message(
        ticker=ticker,
        action=f"{action} (filled at open)",
        fill_price=fill_price,
        alert_price=alert_price,
        qty=qty,
        position_qty=position_qty or 0.0,
        dollar_pnl=dollar_pnl,
        pct_pnl=pct_pnl,
        record_str=record_str,
    )
    await notify_robinhood(message)


async def _schedule_rh_pending_followup(
    order_id: str,
    ticker: str,
    action: str,
    side: str,
    qty: Optional[float],
    alert_price: Optional[float],
    avg_buy_price: Optional[float],
) -> None:
    from app.scheduler import scheduler
    from app.pending_orders import save_pending_order
    from app.trading.alpaca_client import get_next_trading_day

    next_day = get_next_trading_day()
    run_dt = _ET.localize(datetime.combine(next_day, dtime(9, 31)))

    scheduler.add_job(
        notify_rh_pending_fill,
        "date",
        run_date=run_dt,
        args=[order_id, ticker, action, side, qty, alert_price, avg_buy_price],
        id=f"pending_{order_id}",
        replace_existing=True,
    )
    save_pending_order(
        order_id, ticker, action, alert_price, None, run_dt.isoformat(),
        broker="rh", side=side, qty=qty, avg_buy_price=avg_buy_price,
    )
    log.info("RH queued order %s scheduled for resolution at %s", order_id, run_dt)
