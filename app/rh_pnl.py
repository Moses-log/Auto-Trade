from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd
import pytz

from app.chart import generate_rh_equity_chart, generate_rh_pnl_chart
from app.notifications import notify_rh_pnl, notify_rh_pnl_with_chart
from app.pnl import deposit_adjusted_equity, fetch_spy_close_price, format_spy_comparison_lines
from app.investors import get_deposit_events
from app.rh_equity_history import get_snapshots, record_snapshot
from app.rh_trade_record import format_rh_record, get_all_trades, get_totals
from app.trading.robinhood_client import rh_client

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


def _period_start(period: str) -> datetime:
    # Use ET midnight so "daily" aligns with calendar days in Eastern time,
    # not UTC (which would start at 8pm ET the prior evening).
    now_et = datetime.now(ET)
    if period == "daily":
        return now_et.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if period == "weekly":
        days_since_monday = now_et.weekday()
        return (now_et - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if period == "monthly":
        return now_et.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if period == "ytd":
        return now_et.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if period == "1year":
        return datetime.now(timezone.utc) - timedelta(days=365)
    return datetime.min.replace(tzinfo=timezone.utc)  # alltime


def _period_start_date(period: str) -> str:
    """ET calendar date (YYYY-MM-DD) marking the start of `period`, for
    comparing against recorded equity-snapshot dates. "daily" isn't covered
    here — its baseline is the prior recorded snapshot, not a calendar date."""
    now_et = datetime.now(ET)
    if period == "weekly":
        d = (now_et - timedelta(days=now_et.weekday())).date()
    elif period == "monthly":
        d = now_et.date().replace(day=1)
    elif period == "ytd":
        d = now_et.date().replace(month=1, day=1)
    elif period == "1year":
        d = (now_et - timedelta(days=365)).date()
    else:  # alltime
        return "0000-01-01"
    return d.isoformat()


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
        if monday.month == now.month:
            return f"Week of {monday.strftime('%b')} {monday.day}–{now.day}, {now.year}"
        return f"Week of {monday.strftime('%b')} {monday.day} – {now.strftime('%b')} {now.day}, {now.year}"
    if period == "monthly":
        return f"Month of {now.strftime('%B %Y')}"
    if period == "ytd":
        return f"YTD Jan 1–{now.strftime('%b')} {now.day}, {now.year}"
    if period == "1year":
        return f"Trailing 12 Months through {now.strftime('%b %d, %Y')}"
    return "All Time"


def _format_rh_report(
    period_label: str,
    trades: List[dict],
    all_wins: int,
    all_losses: int,
    rh_pct: Optional[float] = None,
    spy_pct: Optional[float] = None,
) -> str:
    period_wins = sum(1 for t in trades if t.get("is_win"))
    period_losses = len(trades) - period_wins
    period_pnl = sum(t.get("dollar_pnl", 0.0) for t in trades)
    all_record = format_rh_record(all_wins, all_losses)

    if not trades:
        lines = [
            f"📊 **RH P&L — {period_label}**",
            "No trades in this period.",
        ]
    else:
        if period_pnl >= 0:
            emoji = "📈🟢"
            pnl_str = f"+${period_pnl:,.2f}"
        else:
            emoji = "📉🔴"
            pnl_str = f"-${abs(period_pnl):,.2f}"

        win_rate = f"{period_wins / len(trades) * 100:.0f}% Win Rate"
        lines = [
            f"{emoji} **RH P&L — {period_label}**",
            f"Trades: {len(trades)} ({period_wins}W / {period_losses}L) | {win_rate}",
            f"P&L: {pnl_str}",
        ]

    if rh_pct is not None:
        rh_sign = "+" if rh_pct >= 0 else ""
        lines.append(f"Portfolio Return: {rh_sign}{rh_pct:.2f}%")
        if spy_pct is not None:
            lines += format_spy_comparison_lines(rh_pct, spy_pct)

    lines.append(f"All-Time Record: {all_record}")

    return "\n".join(lines)


async def record_rh_equity_snapshot() -> None:
    """Record RH equity and SPY's price together as one paired snapshot.

    Called once daily by the scheduler at the same 16:00 ET tick as the
    existing Alpaca reports, so RH-vs-SPY comparisons are always built from
    numbers captured at the same instant and never drift out of sync.
    """
    try:
        equity = await rh_client.get_portfolio_equity_async()
        if equity is None:
            log.warning("record_rh_equity_snapshot: RH equity unavailable, skipping")
            return
        spy_close = fetch_spy_close_price()
        if spy_close is None:
            log.warning("record_rh_equity_snapshot: SPY price unavailable, skipping")
            return
        now_et = datetime.now(ET)
        await record_snapshot(now_et.date().isoformat(), int(now_et.timestamp()), equity, spy_close)
        log.info("RH equity snapshot recorded: equity=%.2f spy_close=%.2f", equity, spy_close)
    except Exception as exc:
        log.error("record_rh_equity_snapshot failed: %s", exc, exc_info=True)


async def send_rh_report(period: str) -> None:
    """Compute and send an RH P&L report for the given period."""
    try:
        since = _period_start(period)
        all_trades = get_all_trades()
        period_trades = _filter_trades(all_trades, since)
        all_wins, all_losses = get_totals()
        label = _period_label(period)

        rh_pct: Optional[float] = None
        spy_pct: Optional[float] = None
        chart = None

        snapshots = get_snapshots()
        if snapshots:
            latest = snapshots[-1]
            if period == "daily":
                baseline = snapshots[-2] if len(snapshots) >= 2 else None
            else:
                start_date = _period_start_date(period)
                baseline = next((s for s in snapshots if s["date"] >= start_date), None)

            if baseline is not None:
                _devts = get_deposit_events()
                period_deposits = sum(
                    amt for dt, amt in _devts
                    if baseline["date"] < dt <= latest["date"]
                )
                rh_adj = latest["equity"] - period_deposits
                if baseline["equity"]:
                    rh_pct = (rh_adj - baseline["equity"]) / baseline["equity"] * 100
                if baseline["spy_close"]:
                    spy_pct = (latest["spy_close"] - baseline["spy_close"]) / baseline["spy_close"] * 100

                if baseline["ts"] != latest["ts"]:
                    period_snapshots = [s for s in snapshots if baseline["ts"] <= s["ts"] <= latest["ts"]]
                    equity = [s["equity"] for s in period_snapshots]
                    timestamps = [s["ts"] for s in period_snapshots]
                    _adj_equity = deposit_adjusted_equity(equity, timestamps, _devts)
                    spy_df = pd.DataFrame(
                        {"Close": [s["spy_close"] for s in period_snapshots]},
                        index=pd.to_datetime(timestamps, unit="s"),
                    )
                    chart = generate_rh_equity_chart(_adj_equity, timestamps, spy_df, f"RH Portfolio vs S&P 500 — {label}")

        msg = _format_rh_report(label, period_trades, all_wins, all_losses, rh_pct=rh_pct, spy_pct=spy_pct)

        if not chart and len(period_trades) >= 2:
            chart = generate_rh_pnl_chart(period_trades, f"RH P&L — {label}")

        if chart:
            await notify_rh_pnl_with_chart(msg, chart)
            log.info("RH %s P&L report with chart sent (%d trades)", period, len(period_trades))
            return

        await notify_rh_pnl(msg)
        log.info("RH %s P&L report sent (%d trades)", period, len(period_trades))
    except Exception as exc:
        log.error("RH %s P&L report failed: %s", period, exc)
        await notify_rh_pnl(f"⚠️ RH {period} P&L report failed: {exc}")
