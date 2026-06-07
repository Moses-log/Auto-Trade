from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytz

from app.notifications import notify_robinhood
from app.rh_trade_record import format_rh_record, get_all_trades, get_totals

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


def _period_start(period: str) -> datetime:
    now = datetime.now(timezone.utc)
    if period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        days_since_monday = now.weekday()
        return (now - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "ytd":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "1year":
        return now - timedelta(days=365)
    return datetime.min.replace(tzinfo=timezone.utc)  # alltime


def _filter_trades(trades: List[dict], since: datetime) -> List[dict]:
    result = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(t["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= since:
                result.append(t)
        except Exception:
            pass
    return result


def _period_label(period: str) -> str:
    now = datetime.now(ET)
    if period == "daily":
        return f"{now.strftime('%A %B')} {now.day}, {now.year}"
    if period == "weekly":
        monday = now - timedelta(days=now.weekday())
        return f"Week of {monday.strftime('%b')} {monday.day}–{now.day}, {now.year}"
    if period == "monthly":
        return f"Month of {now.strftime('%B %Y')}"
    if period == "ytd":
        return f"YTD Jan 1–{now.strftime('%b')} {now.day}, {now.year}"
    if period == "1year":
        return f"Trailing 12 Months through {now.strftime('%b %d, %Y')}"
    return "All Time"


def _format_rh_report(period_label: str, trades: List[dict], all_wins: int, all_losses: int) -> str:
    period_wins = sum(1 for t in trades if t.get("is_win"))
    period_losses = len(trades) - period_wins
    period_pnl = sum(t.get("dollar_pnl", 0.0) for t in trades)

    if period_pnl >= 0:
        emoji = "📈🟢"
        pnl_str = f"+${period_pnl:,.2f}"
    else:
        emoji = "📉🔴"
        pnl_str = f"-${abs(period_pnl):,.2f}"

    period_label_title = period_label.split(" of ")[-1] if " of " in period_label else period_label

    if not trades:
        all_record = format_rh_record(all_wins, all_losses)
        return "\n".join([
            f"📊 **RH P&L — {period_label}**",
            "No trades in this period.",
            f"All-Time Record: {all_record}",
        ])

    win_rate = f"{period_wins / len(trades) * 100:.0f}% Win Rate" if trades else "—"
    lines = [
        f"{emoji} **RH P&L — {period_label}**",
        f"Trades: {len(trades)} ({period_wins}W / {period_losses}L) | {win_rate}",
        f"P&L: {pnl_str}",
    ]

    all_record = format_rh_record(all_wins, all_losses)
    lines.append(f"All-Time Record: {all_record}")

    return "\n".join(lines)


async def send_rh_report(period: str) -> None:
    """Compute and send an RH P&L report for the given period."""
    try:
        since = _period_start(period)
        all_trades = get_all_trades()
        period_trades = _filter_trades(all_trades, since)
        all_wins, all_losses = get_totals()
        label = _period_label(period)
        msg = _format_rh_report(label, period_trades, all_wins, all_losses)
        await notify_robinhood(msg)
        log.info("RH %s P&L report sent (%d trades)", period, len(period_trades))
    except Exception as exc:
        log.error("RH %s P&L report failed: %s", period, exc)
        await notify_robinhood(f"⚠️ RH {period} P&L report failed: {exc}")
