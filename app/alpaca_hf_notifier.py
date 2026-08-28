from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz

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
    head_emoji = _GREEN if (is_win or direction == "LONG") else _RED
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
