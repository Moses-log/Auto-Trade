"""
pnl.py — Daily and weekly P&L calculation and Discord reporting.

Fetches portfolio equity history from Alpaca, computes dollar and
percentage P&L, formats a Discord message, and sends it via notify().

Jobs are registered in scheduler.py and fire at 4pm ET.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz

from app.notifications import notify
from app.trading.alpaca_client import get_portfolio_history

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


@dataclass
class PnLResult:
    period: str        # "daily" or "weekly"
    close_equity: float
    dollar_pnl: float
    pct_pnl: float


def _compute_pnl(history, period: str) -> PnLResult:
    """Compute P&L from a PortfolioHistory object.

    Uses first equity value as open and last as close.
    """
    open_eq = history.equity[0]
    close_eq = history.equity[-1]
    dollar = close_eq - open_eq
    pct = (dollar / open_eq * 100) if open_eq else 0.0
    return PnLResult(period=period, close_equity=close_eq, dollar_pnl=dollar, pct_pnl=pct)


def _format_message(result: PnLResult, label: str, date_str: str) -> str:
    """Format a Discord-ready P&L message string."""
    emoji = "📈" if result.dollar_pnl >= 0 else "📉"
    if result.dollar_pnl >= 0:
        pnl_str = f"+${result.dollar_pnl:,.2f} (+{result.pct_pnl:.2f}%)"
    else:
        pnl_str = f"-${abs(result.dollar_pnl):,.2f} ({result.pct_pnl:.2f}%)"
    return (
        f"{emoji} {label} \u2014 {date_str}\n"
        f"Portfolio: ${result.close_equity:,.2f}\n"
        f"P&L: {pnl_str}"
    )


async def send_daily_report() -> None:
    """Fetch daily portfolio history and post P&L to Discord."""
    now = datetime.now(ET)
    date_str = f"{now.strftime('%A %B')} {now.day}, {now.year}"
    try:
        history = get_portfolio_history(period="1D", timeframe="1Min")
        result = _compute_pnl(history, "daily")
        msg = _format_message(result, "Daily P&L", date_str)
        await notify(msg)
        log.info("Daily P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Daily P&L report failed: %s", exc)
        await notify(f"\u26a0\ufe0f Daily P&L report failed: {exc}")


async def send_weekly_report() -> None:
    """Fetch weekly portfolio history and post P&L to Discord."""
    now = datetime.now(ET)
    monday = now - timedelta(days=now.weekday())
    date_str = f"Week of {monday.strftime('%b')} {monday.day}\u2013{now.day}, {now.year}"
    try:
        history = get_portfolio_history(period="1W", timeframe="1D")
        result = _compute_pnl(history, "weekly")
        msg = _format_message(result, "Weekly P&L", date_str)
        await notify(msg)
        log.info("Weekly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Weekly P&L report failed: %s", exc)
        await notify(f"\u26a0\ufe0f Weekly P&L report failed: {exc}")
