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
import pytz

log = logging.getLogger(__name__)

_CT = pytz.timezone("America/Chicago")


def _d(amount: float) -> str:
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):,.2f}"


def _net_emoji(amount: float) -> str:
    return "🟢" if amount >= 0 else "🔴"


def _tax_timestamp() -> str:
    now = datetime.now(_CT)
    hour = int(now.strftime("%I"))
    return f"🕐 {hour}:{now.strftime('%M %p %Z')} — {now.strftime('%A, %B')} {now.day}, {now.year}"


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


# ── Standalone report senders ─────────────────────────────────────────────────

async def send_alpaca_tax_report(year: int) -> None:
    """Build and post the Alpaca tax summary (with investor breakdown) to the Alpaca tax channel."""
    from app.notifications import notify_alpaca_tax
    from app.investors import load_investors, get_total_deposited

    alpaca = await compute_alpaca_tax_summary(year)
    lines = [f"📋 **Alpaca Tax Summary — {year}**"]

    if "error" in alpaca:
        lines.append(f"> ❌ Could not fetch: {alpaca['error']}")
    else:
        st_net = alpaca["short_term_net"]
        lt_net = alpaca["long_term_net"]
        alpaca_net = st_net + lt_net

        lines.append("**Short-term** *(held < 1 year)*")
        lines.append(f"> Gains:   {_d(alpaca['short_term_gains'])}")
        lines.append(f"> Losses:  {_d(alpaca['short_term_losses'])}")
        lines.append(f"> **Net:   {_d(st_net)} {_net_emoji(st_net)}**")

        lines.append("**Long-term** *(held ≥ 1 year)*")
        if alpaca["long_term_gains"] == 0 and alpaca["long_term_losses"] == 0:
            lines.append("> No long-term trades")
        else:
            lines.append(f"> Gains:   {_d(alpaca['long_term_gains'])}")
            lines.append(f"> Losses:  {_d(alpaca['long_term_losses'])}")
            lines.append(f"> **Net:   {_d(lt_net)} {_net_emoji(lt_net)}**")

        lines.append(f"*{alpaca['sell_event_count']} taxable sells*")
        if alpaca["unknown_basis_count"] > 0:
            lines.append(
                f"> ⚠️ {alpaca['unknown_basis_count']} sell(s) missing cost basis "
                f"— position may have been opened before recorded history"
            )

        try:
            eligible = [(inv, get_total_deposited(inv)) for inv in load_investors()]
            eligible = [(inv, dep) for inv, dep in eligible if dep > 0]
            total_capital = sum(dep for _, dep in eligible)
            if eligible and total_capital > 0:
                lines += ["", "**👥 Investor Breakdown** *(estimated by capital share)*"]
                for inv, inv_dep in eligible:
                    share = inv_dep / total_capital
                    inv_st_net = st_net * share
                    inv_lt_net = lt_net * share
                    lines.append(f"**{inv.name}** *({share * 100:.1f}%)*")
                    lines.append(f"> Short-term Net: **{_d(inv_st_net)}** {_net_emoji(inv_st_net)}")
                    if alpaca["long_term_gains"] != 0 or alpaca["long_term_losses"] != 0:
                        lines.append(f"> Long-term Net:  **{_d(inv_lt_net)}** {_net_emoji(inv_lt_net)}")
        except Exception as exc:
            log.warning("Could not compute investor tax breakdown: %s", exc)

        lines += ["", f"**💰 Alpaca Net Realized: {_d(alpaca_net)} {_net_emoji(alpaca_net)}**"]

    lines += ["⚠️ Estimates only — consult a tax professional.", "", _tax_timestamp()]
    await notify_alpaca_tax("\n".join(lines))


async def send_rh_tax_report(year: int) -> None:
    """Build and post the Robinhood tax summary to the RH tax channel."""
    from app.notifications import notify_rh_tax

    rh = compute_rh_tax_summary(year)
    lines = [f"📋 **Robinhood Tax Summary — {year}**", "*(all short-term — algorithmic)*"]

    if rh["total_trades"] == 0:
        lines.append(f"> No recorded trades for {year}")
    else:
        rh_net = rh["short_term_net"]
        lines.append(f"> Gains:   {_d(rh['short_term_gains'])} *({rh['win_count']} wins)*")
        lines.append(f"> Losses:  {_d(rh['short_term_losses'])} *({rh['loss_count']} losses)*")
        lines.append(f"> **Net:   {_d(rh_net)} {_net_emoji(rh_net)}**")
        lines.append(f"*{rh['total_trades']} total trades*")
        lines += ["", f"**💰 Robinhood Net Realized: {_d(rh_net)} {_net_emoji(rh_net)}**"]

    lines += ["⚠️ Estimates only — consult a tax professional.", "", _tax_timestamp()]
    await notify_rh_tax("\n".join(lines))
