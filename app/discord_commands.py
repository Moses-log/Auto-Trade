from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Optional

import pytz

from app.config import settings
from app.investors import Deposit, get_total_deposited, load_investors, save_investors, investors_lock
from app.notifications import get_http_client
from app.pnl import (
    send_daily_report,
    send_weekly_report,
    send_monthly_report,
    send_yearly_report,
    send_ytd_report,
    send_alltime_report,
    send_investor_report,
)
from app.rh_pnl import send_rh_report
from app.trading.alpaca_client import close_position, get_account, get_all_positions, get_latest_price
from app.trading.robinhood_client import rh_client

_CT = pytz.timezone("America/Chicago")

log = logging.getLogger(__name__)


async def _edit_original(token: str, content: str) -> None:
    url = f"https://discord.com/api/v10/webhooks/{settings.discord_app_id}/{token}/messages/@original"
    try:
        await get_http_client().patch(url, json={"content": content}, timeout=10)
    except Exception as exc:
        log.warning("Failed to edit Discord follow-up: %s", exc)


async def handle_deposit(
    investor_name: str,
    amount: float,
    spy_price: Optional[float],
    token: str,
) -> None:
    if spy_price is not None and spy_price <= 0:
        await _edit_original(token, "❌ SPY price must be positive")
        return

    if spy_price is None:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            await _edit_original(token, "❌ Could not fetch SPY price — provide spy_price manually")
            return

    # Capture result inside lock, await Discord call outside to avoid deadlock
    match_name = None
    async with investors_lock:
        investors = load_investors()
        match = next((inv for inv in investors if inv.name.lower() == investor_name.lower()), None)
        if match is not None:
            match_name = match.name
            match.deposits.append(Deposit(amount=amount, entry_spy=spy_price, date=date.today().isoformat()))
            save_investors(investors)

    if match_name is None:
        await _edit_original(token, f'❌ Investor "{investor_name}" not found — check spelling')
        return
    await _edit_original(token, f"✅ {match_name} — ${amount:,.2f} deposit recorded\nSPY entry: ${spy_price:,.2f}")


async def handle_withdraw(investor_name: str, amount: float, token: str) -> None:
    if amount <= 0:
        await _edit_original(token, "❌ Withdrawal amount must be positive")
        return

    spy_price = get_latest_price("SPY")
    if spy_price is None:
        await _edit_original(token, "❌ Could not fetch SPY price — try again")
        return

    # Capture result inside lock, await Discord calls outside to avoid deadlock
    error_msg = None
    match_name = None
    remaining = None
    async with investors_lock:
        investors = load_investors()
        match = next((inv for inv in investors if inv.name.lower() == investor_name.lower()), None)
        if match is None:
            error_msg = f'❌ Investor "{investor_name}" not found — check spelling'
        else:
            match_name = match.name
            total = get_total_deposited(match)
            if amount > total:
                error_msg = f"❌ Withdrawal ${amount:,.2f} exceeds {match_name} total ${total:,.2f}"
            else:
                match.deposits.append(Deposit(amount=-amount, entry_spy=spy_price, date=date.today().isoformat()))
                save_investors(investors)
                remaining = get_total_deposited(match)

    if error_msg:
        await _edit_original(token, error_msg)
        return
    await _edit_original(token, f"✅ {match_name} — ${amount:,.2f} withdrawal recorded\nSPY @ ${spy_price:,.2f}\nRemaining deposited: ${remaining:,.2f}")


async def handle_report(broker: str, report_type: str, token: str) -> None:
    _RH_VALID = {"daily", "weekly", "monthly", "ytd", "1year", "alltime"}
    _VALID = {"daily", "weekly", "monthly", "ytd", "1year", "alltime", "both", "investors"}

    if broker == "robinhood":
        if report_type not in _RH_VALID:
            await _edit_original(token, f"❌ Unknown RH report type: {report_type!r}. Valid: {', '.join(sorted(_RH_VALID))}")
            return
        await send_rh_report(report_type)
        await _edit_original(token, f"✅ RH {report_type.capitalize()} report sent")
        return

    # Alpaca (default)
    if report_type not in _VALID:
        await _edit_original(token, f"❌ Unknown report type: {report_type!r}")
        return
    if report_type in ("daily", "both"):
        await send_daily_report()
    if report_type in ("weekly", "both"):
        await send_weekly_report()
    if report_type == "monthly":
        await send_monthly_report()
    if report_type == "ytd":
        await send_ytd_report()
    if report_type == "1year":
        await send_yearly_report()
    if report_type == "alltime":
        await send_alltime_report()
    if report_type == "investors":
        await send_investor_report()
    await _edit_original(token, f"✅ {report_type.capitalize()} report sent")


def _ct_timestamp() -> str:
    now = datetime.now(_CT)
    hour = int(now.strftime("%I"))
    return f"🕐 {hour}:{now.strftime('%M %p')} {now.strftime('%Z')} — {now.strftime('%A, %B')} {now.day}, {now.year}"


async def handle_status(token: str) -> None:
    loop = asyncio.get_running_loop()

    alpaca_ok = True
    alpaca_account = None
    alpaca_positions = []
    try:
        alpaca_account = await loop.run_in_executor(None, get_account)
        alpaca_positions = await loop.run_in_executor(None, get_all_positions)
    except Exception as exc:
        alpaca_ok = False
        log.warning("Status: Alpaca fetch failed: %s", exc)

    rh_positions = await rh_client.get_all_positions_async()
    rh_cash = await rh_client.get_buying_power_async()

    lines = []

    if alpaca_ok and alpaca_account:
        equity = float(alpaca_account.equity or 0)
        cash = float(alpaca_account.cash or 0)
        lines.append("📊 **ALPACA** — 🟢 Live")
        lines.append(f"Equity: ${equity:,.2f}  |  Cash: ${cash:,.2f}")
        if alpaca_positions:
            for pos in alpaca_positions:
                pl = float(pos.unrealized_pl or 0)
                plpc = float(pos.unrealized_plpc or 0) * 100
                sign = "+" if pl >= 0 else ""
                lines.append(f"  📍 {pos.symbol} · {float(pos.qty):g} sh · {sign}${pl:,.2f} ({sign}{plpc:.2f}%)")
        else:
            lines.append("  No open positions")
    else:
        lines.append("📊 **ALPACA** — 🔴 Unavailable")

    lines.append("")

    rh_status = "🟢 Session Active" if rh_client.available else "🔴 Session Offline"
    lines.append(f"🤖 **ROBINHOOD** — {rh_status}")
    if rh_cash is not None:
        lines.append(f"Cash: ${rh_cash:,.2f}")
    if rh_positions:
        for pos in rh_positions:
            pl = pos.get("unrealized_pl", 0.0)
            plpc = pos.get("unrealized_plpc", 0.0)
            sign = "+" if pl >= 0 else ""
            lines.append(f"  📍 {pos['symbol']} · {pos['qty']:g} sh · {sign}${pl:,.2f} ({sign}{plpc:.2f}%)")
    elif rh_client.available:
        lines.append("  No open positions")
    else:
        lines.append("  (session offline)")

    lines.append("")
    lines.append(_ct_timestamp())
    await _edit_original(token, "\n".join(lines))


async def handle_positions(broker: str, token: str) -> None:
    loop = asyncio.get_running_loop()
    show_alpaca = broker in ("alpaca", "both")
    show_rh = broker in ("robinhood", "both")

    lines = ["📈 **Open Positions**", ""]

    if show_alpaca:
        lines.append("**Alpaca**")
        try:
            positions = await loop.run_in_executor(None, get_all_positions)
            if positions:
                for pos in positions:
                    qty = float(pos.qty)
                    entry = float(pos.avg_entry_price or 0)
                    current = float(pos.current_price or 0)
                    pl = float(pos.unrealized_pl or 0)
                    plpc = float(pos.unrealized_plpc or 0) * 100
                    sign = "+" if pl >= 0 else ""
                    lines.append(
                        f"  {pos.symbol} · {qty:g} sh · "
                        f"Entry ${entry:,.2f} → ${current:,.2f} · "
                        f"{sign}${pl:,.2f} ({sign}{plpc:.2f}%)"
                    )
            else:
                lines.append("  No open positions")
        except Exception as exc:
            lines.append(f"  ❌ Could not fetch: {exc}")
        lines.append("")

    if show_rh:
        lines.append("**Robinhood**")
        if not rh_client.available:
            lines.append("  🔴 Session offline")
        else:
            positions = await rh_client.get_all_positions_async()
            if positions:
                for pos in positions:
                    qty = pos.get("qty", 0)
                    entry = pos.get("avg_entry_price", 0.0)
                    current = pos.get("current_price", 0.0)
                    pl = pos.get("unrealized_pl", 0.0)
                    plpc = pos.get("unrealized_plpc", 0.0)
                    sign = "+" if pl >= 0 else ""
                    lines.append(
                        f"  {pos['symbol']} · {qty:g} sh · "
                        f"Entry ${entry:,.2f} → ${current:,.2f} · "
                        f"{sign}${pl:,.2f} ({sign}{plpc:.2f}%)"
                    )
            else:
                lines.append("  No open positions")
        lines.append("")

    lines.append(_ct_timestamp())
    await _edit_original(token, "\n".join(lines))


async def handle_close(ticker: str, broker: str, token: str) -> None:
    loop = asyncio.get_running_loop()
    ticker = ticker.upper()
    close_alpaca = broker in ("alpaca", "both")
    close_rh = broker in ("robinhood", "both")

    lines = [f"🔴 **CLOSE {ticker}**", ""]

    if close_alpaca:
        try:
            order = await loop.run_in_executor(None, close_position, ticker)
            if order is None:
                lines.append("**Alpaca:** No position to close")
            else:
                qty = float(order.qty or 0)
                lines.append(f"**Alpaca:** Closed {qty:g} shares ✅")
        except Exception as exc:
            lines.append(f"**Alpaca:** ❌ Failed — {exc}")

    if close_rh:
        result = await rh_client.close_ticker_async(ticker)
        status = result.get("status")
        if status == "ok":
            note = result.get("note")
            qty = result.get("qty", 0)
            if note:
                lines.append(f"**Robinhood:** {note}")
            else:
                lines.append(f"**Robinhood:** Closed {qty:g} shares ✅")
        elif status == "skipped":
            lines.append(f"**Robinhood:** Skipped — {result.get('reason', 'session unavailable')}")
        else:
            lines.append(f"**Robinhood:** ❌ Failed — {result.get('reason', 'unknown error')}")

    lines.append("")
    lines.append(_ct_timestamp())
    await _edit_original(token, "\n".join(lines))


async def dispatch_command(command: str, options: dict, token: str) -> None:
    try:
        if command == "deposit":
            await handle_deposit(
                investor_name=options["investor"],
                amount=float(options["amount"]),
                spy_price=float(options["spy_price"]) if "spy_price" in options else None,
                token=token,
            )
        elif command == "withdraw":
            await handle_withdraw(
                investor_name=options["investor"],
                amount=float(options["amount"]),
                token=token,
            )
        elif command == "report":
            broker = options.get("_subcommand", "alpaca")
            await handle_report(broker=broker, report_type=options.get("type", "daily"), token=token)
        elif command == "status":
            await handle_status(token=token)
        elif command == "positions":
            await handle_positions(broker=options.get("broker", "both"), token=token)
        elif command == "close":
            ticker = options.get("ticker", "")
            if not ticker:
                await _edit_original(token, "❌ Ticker is required")
            else:
                await handle_close(ticker=ticker, broker=options.get("broker", "both"), token=token)
        else:
            await _edit_original(token, f"❌ Unknown command: {command}")
    except Exception as exc:
        log.exception("Command %s failed", command)
        await _edit_original(token, f"❌ Command failed: {exc}")
