from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Optional

import pytz

from app.config import settings
from app.investors import (
    Deposit,
    Investor,
    compute_nav_per_unit,
    load_investors,
    save_investors,
    investors_lock,
)
from app.pending_withdrawals import load_pending_withdrawals
from app.notifications import get_http_client
from app.pnl import (
    send_daily_report,
    send_weekly_report,
    send_monthly_report,
    send_yearly_report,
    send_ytd_report,
    send_alltime_report,
    send_inception_report,
    send_custom_report,
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
        await get_http_client().patch(url, json={"content": content[:1990]}, timeout=10)
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

    manual_override = spy_price is not None
    if not manual_override:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            await _edit_original(token, "❌ Could not fetch SPY price — provide spy_price manually")
            return

    # Capture result inside lock, await Discord call outside to avoid deadlock
    is_new = False
    async with investors_lock:
        investors = load_investors()
        match = next((inv for inv in investors if inv.name.lower() == investor_name.lower()), None)
        if match is None:
            match = Investor(name=investor_name, deposits=[])
            investors.append(match)
            is_new = True

        if manual_override:
            entry_price = spy_price
        else:
            # match.deposits has NOT had the new deposit appended yet at this point,
            # so this sum is exactly "all units outstanding before this deposit" --
            # including match's own prior deposits if they're an existing investor.
            total_existing_units = sum(
                d.amount / d.entry_spy for inv in investors for d in inv.deposits if d.entry_spy
            )
            if total_existing_units <= 0:
                # Bootstrap case — no real performance to benchmark against yet.
                entry_price = spy_price
            else:
                account = get_account()
                real_total_equity = float(account.equity)
                entry_price = compute_nav_per_unit(investors, real_total_equity)

        match.deposits.append(Deposit(amount=amount, entry_spy=entry_price, date=date.today().isoformat()))
        save_investors(investors)

    status = "🆕 New investor added" if is_new else "✅ Deposit recorded"
    await _edit_original(
        token,
        f"{status} — {match.name}\n${amount:,.2f} @ NAV ${entry_price:,.2f}/unit (SPY ${spy_price:,.2f})",
    )


async def handle_withdraw(investor_name: str, amount: float, token: str, spy_price: Optional[float] = None) -> None:
    from app.withdrawal_execution import schedule_withdrawal, WithdrawalValidationError

    if spy_price is not None and spy_price <= 0:
        await _edit_original(token, "❌ SPY price must be positive")
        return

    try:
        record = await schedule_withdrawal(investor_name, amount, spy_price=spy_price)
    except WithdrawalValidationError as exc:
        await _edit_original(token, f"❌ {exc}")
        return

    run_at_local = datetime.fromisoformat(record["run_at"]).astimezone(_CT)
    spy_note = f" (SPY locked at ${spy_price:,.2f})" if spy_price else ""
    msg = (
        f"⏳ **Withdrawal Scheduled** — {record['investor']}\n"
        f"${record['amount']:,.2f}{spy_note} will be processed at "
        f"{run_at_local.strftime('%b %d, %Y %I:%M %p %Z')}.\n"
        f"Run `/cancel-withdrawal id={record['id']}` to cancel."
    )

    from app.notifications import notify_investors
    asyncio.create_task(notify_investors(msg))
    await _edit_original(token, msg)


async def handle_cancel_withdrawal(withdrawal_id: str, token: str) -> None:
    from app.withdrawal_execution import cancel_pending_withdrawal, WithdrawalNotFoundError

    try:
        record = await cancel_pending_withdrawal(withdrawal_id)
    except WithdrawalNotFoundError as exc:
        await _edit_original(token, f"❌ {exc}")
        return

    await _edit_original(
        token,
        f"✅ Canceled withdrawal `{withdrawal_id}` — ${record['amount']:,.2f} for {record['investor']}.",
    )


async def handle_pending_withdrawals(token: str) -> None:
    records = load_pending_withdrawals()
    if not records:
        await _edit_original(token, "No pending withdrawals.")
        return

    lines = ["⏳ **Pending Withdrawals**", ""]
    for r in records:
        run_at_local = datetime.fromisoformat(r["run_at"]).astimezone(_CT)
        lines.append(
            f"`{r['id']}` — {r['investor']}: ${r['amount']:,.2f}"
            f" (scheduled {run_at_local.strftime('%b %d, %I:%M %p %Z')})"
        )
    await _edit_original(token, "\n".join(lines))


async def handle_report(broker: str, report_type: str, token: str, custom_date: Optional[str] = None) -> None:
    _RH_VALID = {"daily", "weekly", "monthly", "ytd", "1year", "alltime"}
    _VALID = {"daily", "weekly", "monthly", "ytd", "1year", "alltime", "inception", "custom", "both", "investors"}

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

    if report_type == "custom":
        if not custom_date:
            await _edit_original(token, "❌ Custom report requires a `date` option (YYYY-MM-DD)")
            return
        try:
            parsed_date = datetime.strptime(custom_date, "%Y-%m-%d").date()
        except ValueError:
            await _edit_original(token, f"❌ Invalid date {custom_date!r} — use YYYY-MM-DD")
            return
        if parsed_date > date.today():
            await _edit_original(token, "❌ Date cannot be in the future")
            return
        await send_custom_report(parsed_date)
        await _edit_original(token, f"✅ Custom report sent (since {parsed_date.isoformat()})")
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
    if report_type == "inception":
        await send_inception_report()
    if report_type == "investors":
        await send_investor_report()
    await _edit_original(token, f"✅ {report_type.capitalize()} report sent")


def _ct_timestamp() -> str:
    now = datetime.now(_CT)
    hour = int(now.strftime("%I"))
    return f"🕐 {hour}:{now.strftime('%M %p')} {now.strftime('%Z')} — {now.strftime('%A, %B')} {now.day}, {now.year}"


def _fmt_qty(qty: float) -> str:
    """7.0 → '7'  ·  7.5769 → '7.5769'  ·  7.57693000 → '7.5769'"""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.4f}".rstrip("0")


def _fmt_pl(pl: float, plpc: float) -> str:
    """🟢 +$45.23 (+0.80%)  or  🔴 -$45.23 (-0.80%)"""
    emoji = "🟢" if pl >= 0 else "🔴"
    dollar = f"+${pl:,.2f}" if pl >= 0 else f"-${abs(pl):,.2f}"
    pct = f"+{plpc:.2f}%" if plpc >= 0 else f"{plpc:.2f}%"
    return f"{emoji} {dollar} ({pct})"


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
                lines.append(f"  📍 {pos.symbol} · {_fmt_qty(float(pos.qty))} sh · {_fmt_pl(pl, plpc)}")
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
            lines.append(f"  📍 {pos['symbol']} · {_fmt_qty(pos['qty'])} sh · {_fmt_pl(pl, plpc)}")
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

    lines = ["📈 **Open Positions**"]

    if show_alpaca:
        lines.append("")
        lines.append("**📊 Alpaca**")
        try:
            positions = await loop.run_in_executor(None, get_all_positions)
            if positions:
                for pos in positions:
                    qty = float(pos.qty)
                    entry = float(pos.avg_entry_price or 0)
                    current = float(pos.current_price or 0)
                    pl = float(pos.unrealized_pl or 0)
                    plpc = float(pos.unrealized_plpc or 0) * 100
                    lines.append(f"> **{pos.symbol}** — {_fmt_qty(qty)} shares @ ${current:,.2f}")
                    lines.append(f"> {_fmt_pl(pl, plpc)}  ·  Entry ${entry:,.2f}")
            else:
                lines.append("> No open positions")
        except Exception as exc:
            lines.append(f"> ❌ Could not fetch: {exc}")

    if show_rh:
        lines.append("")
        lines.append("**🤖 Robinhood**")
        if not rh_client.available:
            lines.append("> 🔴 Session offline")
        else:
            positions = await rh_client.get_all_positions_async()
            if positions:
                for pos in positions:
                    qty = pos.get("qty", 0.0)
                    entry = pos.get("avg_entry_price", 0.0)
                    current = pos.get("current_price", 0.0)
                    pl = pos.get("unrealized_pl", 0.0)
                    plpc = pos.get("unrealized_plpc", 0.0)
                    lines.append(f"> **{pos['symbol']}** — {_fmt_qty(qty)} shares @ ${current:,.2f}")
                    lines.append(f"> {_fmt_pl(pl, plpc)}  ·  Entry ${entry:,.2f}")
            else:
                lines.append("> No open positions")

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


def _d(amount: float) -> str:
    """Format a dollar amount: +$1,234.56 or -$1,234.56."""
    return f"+${amount:,.2f}" if amount >= 0 else f"-${abs(amount):,.2f}"


async def handle_rebalance(token: str) -> None:
    await _edit_original(token, "🤖 **CLAUDE PORTFOLIO REBALANCE STARTED**\nRunning analysis — watch this channel for updates...")
    from app.claude_manager import run_monthly_rebalance
    await run_monthly_rebalance()


async def handle_portfolio(token: str) -> None:
    await _edit_original(token, "📊 Generating portfolio snapshot — check the snapshot channel...")
    from app.portfolio_report import send_portfolio_snapshot
    await send_portfolio_snapshot()


async def handle_tax_alpaca(year: int, token: str) -> None:
    from app.tax import send_alpaca_tax_report
    await _edit_original(token, f"⏳ Fetching Alpaca {year} trade history…")
    await send_alpaca_tax_report(year)
    await _edit_original(token, f"✅ Alpaca tax summary for {year} posted to channel.")


async def handle_tax_robinhood(year: int, token: str) -> None:
    from app.tax import send_rh_tax_report
    await _edit_original(token, f"⏳ Computing Robinhood {year} tax summary…")
    await send_rh_tax_report(year)
    await _edit_original(token, f"✅ Robinhood tax summary for {year} posted to channel.")


async def handle_rh_deposit(
    token: str,
    amount: Optional[float] = None,
    deposit_date: Optional[str] = None,
    clear: bool = False,
) -> None:
    if clear:
        try:
            from app.rh_deposit_log import clear_rh_deposits
            count = clear_rh_deposits()
        except Exception as exc:
            await _edit_original(token, f"❌ Failed to clear deposits: {exc}")
            return
        await _edit_original(token, f"🗑️ Cleared {count} logged RH deposit(s). Track record chart reset.")
        return

    if amount is None or amount <= 0:
        await _edit_original(token, "❌ Amount must be positive (or use clear: True to remove all)")
        return
    if deposit_date:
        try:
            datetime.strptime(deposit_date, "%Y-%m-%d")
        except ValueError:
            await _edit_original(token, f"❌ Invalid date {deposit_date!r} — use YYYY-MM-DD")
            return
    else:
        deposit_date = date.today().isoformat()

    try:
        from app.rh_deposit_log import append_rh_deposit
        append_rh_deposit(deposit_date, amount)
    except Exception as exc:
        await _edit_original(token, f"❌ Failed to save deposit: {exc}")
        return

    await _edit_original(
        token,
        f"✅ RH deposit logged — ${amount:,.2f} on {deposit_date}\n"
        "The Kimi Portfolio Manager track record chart will now exclude this from gains."
    )


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
                spy_price=float(options["spy_price"]) if "spy_price" in options else None,
                token=token,
            )
        elif command == "cancel-withdrawal":
            await handle_cancel_withdrawal(withdrawal_id=options["id"], token=token)
        elif command == "pending-withdrawals":
            await handle_pending_withdrawals(token=token)
        elif command == "report":
            broker = options.get("_subcommand", "alpaca")
            if broker == "custom":
                await handle_report(
                    broker="alpaca",
                    report_type="custom",
                    token=token,
                    custom_date=options.get("date"),
                )
            else:
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
        elif command == "rh_deposit":
            await handle_rh_deposit(
                token=token,
                amount=float(options["amount"]) if "amount" in options else None,
                deposit_date=options.get("date"),
                clear=bool(options.get("clear", False)),
            )
        elif command == "rebalance":
            await handle_rebalance(token=token)
        elif command == "portfolio":
            await handle_portfolio(token=token)
        elif command == "tax":
            sub = options.get("_subcommand", "alpaca")
            year = int(options.get("year") or datetime.now().year)
            if sub == "robinhood":
                await handle_tax_robinhood(year=year, token=token)
            else:
                await handle_tax_alpaca(year=year, token=token)
        else:
            await _edit_original(token, f"❌ Unknown command: {command}")
    except Exception as exc:
        log.exception("Command %s failed", command)
        await _edit_original(token, f"❌ Command failed: {exc}")
