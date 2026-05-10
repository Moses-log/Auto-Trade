"""
pnl.py — Daily and weekly P&L calculation and Discord reporting.

Fetches portfolio equity history from Alpaca, computes dollar and
percentage P&L, formats a Discord message, and sends it via notify().

Jobs are registered in scheduler.py and fire at 4pm ET.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pytz

from app.investors import compute_breakdown, format_discord_message, load_investors
from app.notifications import notify, notify_investors
from app.trading.alpaca_client import get_latest_price, get_portfolio_history, get_spy_bars

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


def compute_spy_pct(bars) -> Optional[float]:
    """Extract S&P 500 % return from SPY BarSet.

    Args:
        bars: BarSet (dict-like) from get_spy_bars(). Access via bars["SPY"].

    Returns:
        % return as float, or None if bars empty or key missing.
    """
    try:
        spy_bars = bars["SPY"]
    except (KeyError, TypeError):
        return None
    if not spy_bars:
        return None
    open_price = spy_bars[0].open
    close_price = spy_bars[-1].close
    if not open_price:
        return None
    return (close_price - open_price) / open_price * 100


def _format_message(result: PnLResult, label: str, date_str: str, spy_pct: Optional[float] = None) -> str:
    """Format a Discord-ready P&L message string.

    Args:
        result:   PnLResult with portfolio P&L data.
        label:    "Daily P&L" or "Weekly P&L".
        date_str: Human-readable date string.
        spy_pct:  S&P 500 % return for the period. Omits S&P line if None.
    """
    emoji = "📈🟢" if result.dollar_pnl >= 0 else "📉🔴"
    if result.dollar_pnl >= 0:
        pnl_str = f"+${result.dollar_pnl:,.2f} (+{result.pct_pnl:.2f}%)"
    else:
        pnl_str = f"-${abs(result.dollar_pnl):,.2f} ({result.pct_pnl:.2f}%)"

    msg = (
        f"{emoji} {label} \u2014 {date_str}\n"
        f"Portfolio: ${result.close_equity:,.2f}\n"
        f"P&L: {pnl_str}"
    )

    if spy_pct is not None:
        spy_sign = "+" if spy_pct >= 0 else ""
        relative = result.pct_pnl - spy_pct
        rel_sign = "+" if relative >= 0 else ""
        rel_word = "ahead" if relative >= 0 else "behind"
        msg += f"\nS&P 500: {spy_sign}{spy_pct:.2f}% ({rel_sign}{relative:.2f}% {rel_word})"

    return msg


async def send_daily_report() -> None:
    """Fetch daily portfolio history and post P&L to Discord."""
    now = datetime.now(ET)
    date_str = f"{now.strftime('%A %B')} {now.day}, {now.year}"
    try:
        history = get_portfolio_history(period="1D", timeframe="1Min")
        result = _compute_pnl(history, "daily")

        spy_pct = None
        try:
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            bars = get_spy_bars(start=market_open, end=now)
            spy_pct = compute_spy_pct(bars)
        except Exception as spy_exc:
            log.warning("SPY data fetch failed, omitting S&P line: %s", spy_exc)

        msg = _format_message(result, "Daily P&L", date_str, spy_pct=spy_pct)
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

        spy_pct = None
        try:
            monday_open = monday.replace(hour=9, minute=30, second=0, microsecond=0)
            bars = get_spy_bars(start=monday_open, end=now)
            spy_pct = compute_spy_pct(bars)
        except Exception as spy_exc:
            log.warning("SPY data fetch failed, omitting S&P line: %s", spy_exc)

        msg = _format_message(result, "Weekly P&L", date_str, spy_pct=spy_pct)
        await notify(msg)
        log.info("Weekly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Weekly P&L report failed: %s", exc)
        await notify(f"\u26a0\ufe0f Weekly P&L report failed: {exc}")


async def send_investor_report() -> None:
    investors = load_investors()
    if not investors:
        log.warning("No investors found; skipping investor report")
        return

    spy_price = get_latest_price("SPY")
    if spy_price is None:
        log.warning("Could not fetch SPY price; skipping investor report")
        return

    now = datetime.now(ET)
    date_str = now.strftime(f"%B {now.day}, %Y")
    breakdown = compute_breakdown(investors, spy_price)
    message = format_discord_message(breakdown, date_str)
    await notify_investors(message)
    log.info("Investor report sent for %s", date_str)
