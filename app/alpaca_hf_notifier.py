from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytz

from app.trading.alpaca_client import get_orders_filled_range, get_account
from app.investors import load_investors, compute_breakdown
from app.notifications import notify_hf_trade, notify_hf_recap
from app import alpaca_hf_record as rec

log = logging.getLogger(__name__)
CT = pytz.timezone("America/Chicago")

_GREEN = "\U0001F7E2"  # green circle
_RED = "\U0001F534"    # red circle


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _time_ct(ts_ct: datetime) -> str:
    hour = int(ts_ct.strftime("%I"))
    return f"{hour}:{ts_ct.strftime('%M %p')} {ts_ct.strftime('%Z')} — {ts_ct.strftime('%B')} {ts_ct.day}, {ts_ct.year}"


def format_open(symbol, direction, qty, price, ts_ct: datetime) -> str:
    emoji = _GREEN if direction == "LONG" else _RED
    notional = price * qty
    return "\n".join([
        f"{emoji} **{direction} OPEN — {symbol}**",
        f"{qty:g} shares @ {_money(price)} ({_money(notional)})",
        f"\U0001F550 {_time_ct(ts_ct)}",
    ])


def format_close(symbol, direction, qty, exit_price, realized_pnl, pct,
                 is_win: Optional[bool], investor_split, ts_ct: datetime) -> str:
    notional = exit_price * qty
    if is_win is None:
        verdict = ""
        pnl_line = "P&L: n/a (no recorded entry)"
        split_lines = []
    else:
        verdict = "  WIN" if is_win else "  LOSS"
        pnl_line = f"P&L: {_signed(realized_pnl)} ({'+' if pct >= 0 else '-'}{abs(pct):.2f}%)"
        split_lines = ["Investor split:"] + [
            f"  - {name}: {_signed(amt)}" for name, amt in investor_split
        ]
    head_emoji = _GREEN if (is_win is True or (is_win is None and direction == "LONG")) else _RED
    lines = [
        f"{head_emoji} **{direction} CLOSE — {symbol}**{verdict}",
        f"Exit: {qty:g} shares @ {_money(exit_price)} ({_money(notional)})",
        pnl_line,
        f"\U0001F550 {_time_ct(ts_ct)}",
    ] + split_lines
    return "\n".join(lines)


def format_recap(day_label, fills, wins, losses, total_pnl) -> str:
    opens = sum(1 for f in fills if f.get("role") == "OPEN")
    closes = sum(1 for f in fills if f.get("role") == "CLOSE")
    total = wins + losses
    win_rate = (wins / total * 100) if total else 0.0
    lines = [
        f"**Non-SPY Recap — {day_label} (CT)**",
        f"Fills today: {len(fills)}  ({opens} opens / {closes} closes)",
        f"Closed round-trips: {total} — {wins} W / {losses} L ({win_rate:.1f}% win rate)",
        f"Total realized P&L: {_signed(total_pnl)}",
        "Fills:",
    ]
    for f in fills:
        if f.get("role") == "CLOSE" and "realized_pnl" in f:
            tail = f"({_signed(f['realized_pnl'])})"
        else:
            tail = f"({_money(f.get('notional', 0.0))})"
        lines.append(
            f"  {f['symbol']} {f.get('direction','')} {f.get('role','')} "
            f"{f.get('qty', 0):g} @ {_money(f.get('price', 0.0))} {tail}"
        )
    return "\n".join(lines)


_poll_lock = asyncio.Lock()

_INTENT_MAP = {
    "buy_to_open": ("LONG", "OPEN"),
    "sell_to_close": ("LONG", "CLOSE"),
    "sell_to_open": ("SHORT", "OPEN"),
    "buy_to_close": ("SHORT", "CLOSE"),
}


def _classify(order):
    pi = getattr(order, "position_intent", None)
    intent = getattr(pi, "value", pi)
    if intent in _INTENT_MAP:
        return _INTENT_MAP[intent]
    log.warning("Unclassifiable order %s (intent=%s)", getattr(order, "id", "?"), intent)
    return None


async def _investor_split(realized_pnl: float):
    try:
        investors = load_investors()
        if not investors:
            return []
        equity = float(get_account().equity)
        breakdown = compute_breakdown(investors, 0.0, equity)
        return [(r.name, realized_pnl * r.portfolio_share / 100.0)
                for r in breakdown.investors]
    except Exception as exc:
        log.warning("Investor split failed: %s", exc)
        return []


async def poll_and_notify() -> None:
    async with _poll_lock:
        now = datetime.now(timezone.utc)
        last = await rec.get_last_seen()
        if last is None:
            await rec.set_last_seen(now)  # seed; no backfill
            return
        after = last - timedelta(minutes=5)
        try:
            orders = get_orders_filled_range(after, now)
        except Exception as exc:
            log.warning("HF poll fetch failed: %s", exc)
            return

        newest = last
        for order in orders:
            oid = str(order.id)
            if order.symbol == "SPY":
                continue
            if await rec.is_seen(oid):
                continue
            classified = _classify(order)
            if classified is None:
                await rec.mark_seen(oid)
                continue
            direction, role = classified
            qty = float(order.filled_qty or 0)
            if qty <= 0:
                await rec.mark_seen(oid)
                continue
            price = float(order.filled_avg_price or 0)
            filled_at = order.filled_at or now
            ts_ct = filled_at.astimezone(CT)
            ts_iso = filled_at.isoformat()

            if role == "OPEN":
                await rec.record_open(order.symbol, direction, qty, price, ts_iso, oid)
                await rec.record_daily_fill({
                    "symbol": order.symbol, "role": "OPEN", "direction": direction,
                    "qty": qty, "price": price, "notional": price * qty, "ts": ts_iso,
                })
                await notify_hf_trade(format_open(order.symbol, direction, qty, price, ts_ct))
            else:
                result = await rec.record_close(order.symbol, direction, qty, price, ts_iso)
                split = await _investor_split(result.realized_pnl) if result.is_win is not None else []
                await rec.record_daily_fill({
                    "symbol": order.symbol, "role": "CLOSE", "direction": direction,
                    "qty": qty, "price": price, "realized_pnl": result.realized_pnl,
                    "is_win": result.is_win, "ts": ts_iso,
                })
                await notify_hf_trade(format_close(
                    order.symbol, direction, qty, price,
                    result.realized_pnl, result.pct, result.is_win, split, ts_ct,
                ))

            await rec.mark_seen(oid)
            if order.filled_at and order.filled_at > newest:
                newest = order.filled_at

        await rec.set_last_seen(min(newest, now))


def _day_label_ct() -> str:
    now = datetime.now(CT)
    return f"{now.strftime('%B')} {now.day}, {now.year}"


async def send_daily_recap() -> None:
    fills = await rec.pop_daily_fills()
    closes = [f for f in fills if f.get("role") == "CLOSE"]
    wins = sum(1 for f in closes if f.get("is_win") is True)
    losses = sum(1 for f in closes if f.get("is_win") is False)
    total_pnl = sum(f.get("realized_pnl", 0.0) for f in closes)
    await notify_hf_recap(format_recap(_day_label_ct(), fills, wins, losses, total_pnl))
