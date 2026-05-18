from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import httpx

from app.config import settings
from app.investors import Deposit, get_total_deposited, load_investors, save_investors
from app.pnl import (
    send_daily_report,
    send_weekly_report,
    send_monthly_report,
    send_yearly_report,
    send_ytd_report,
    send_alltime_report,
    send_investor_report,
)
from app.trading.alpaca_client import get_latest_price

log = logging.getLogger(__name__)


async def _edit_original(token: str, content: str) -> None:
    url = f"https://discord.com/api/v10/webhooks/{settings.discord_app_id}/{token}/messages/@original"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.patch(url, json={"content": content})
    except Exception as exc:
        log.warning("Failed to edit Discord follow-up: %s", exc)


async def handle_deposit(
    investor_name: str,
    amount: float,
    spy_price: Optional[float],
    token: str,
) -> None:
    investors = load_investors()
    match = next((inv for inv in investors if inv.name.lower() == investor_name.lower()), None)

    if match is None:
        await _edit_original(token, f'❌ Investor "{investor_name}" not found — check spelling')
        return

    if spy_price is None:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            await _edit_original(token, "❌ Could not fetch SPY price — provide spy_price manually")
            return

    match.deposits.append(Deposit(amount=amount, entry_spy=spy_price, date=date.today().isoformat()))
    save_investors(investors)
    await _edit_original(token, f"✅ {match.name} — ${amount:,.2f} deposit recorded\nSPY entry: ${spy_price:,.2f}")


async def handle_withdraw(investor_name: str, amount: float, token: str) -> None:
    investors = load_investors()
    match = next((inv for inv in investors if inv.name.lower() == investor_name.lower()), None)

    if match is None:
        await _edit_original(token, f'❌ Investor "{investor_name}" not found — check spelling')
        return

    total = get_total_deposited(match)
    if amount > total:
        await _edit_original(
            token,
            f"❌ Withdrawal ${amount:,.2f} exceeds {match.name} total ${total:,.2f}"
        )
        return

    spy_price = get_latest_price("SPY")
    if spy_price is None:
        await _edit_original(token, "❌ Could not fetch SPY price — try again")
        return

    match.deposits.append(Deposit(amount=-amount, entry_spy=spy_price, date=date.today().isoformat()))
    save_investors(investors)
    remaining = get_total_deposited(match)
    await _edit_original(token, f"✅ {match.name} — ${amount:,.2f} withdrawal recorded\nSPY @ ${spy_price:,.2f}\nRemaining deposited: ${remaining:,.2f}")


async def handle_report(report_type: str, token: str) -> None:
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
            await handle_report(report_type=options["type"], token=token)
        else:
            await _edit_original(token, f"❌ Unknown command: {command}")
    except Exception as exc:
        log.exception("Command %s failed", command)
        await _edit_original(token, f"❌ Command failed: {exc}")
