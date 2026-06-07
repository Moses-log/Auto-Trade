from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz

from app.notifications import notify_robinhood, notify_rh_session
from app.rh_trade_record import format_rh_record, record_rh_trade

log = logging.getLogger(__name__)
CT = pytz.timezone("America/Chicago")

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
                    "⚠️ Robinhood session expired — POST /robinhood-auth to re-authenticate"
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
            message = _format_rh_queued_message(
                ticker=ticker,
                action=action,
                qty=rh_result.get("qty"),
                price_est=rh_result.get("price_est") or alert_price,
            )
            await notify_robinhood(message)
            return

        # "no position to close" — no P&L to show
        if rh_result.get("note"):
            await notify_robinhood(
                f"ℹ️ RH {action.upper()} {ticker}: {rh_result['note']}"
            )
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
