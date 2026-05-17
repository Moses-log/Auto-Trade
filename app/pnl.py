"""
pnl.py — Daily and weekly P&L calculation and Discord reporting.

Fetches portfolio equity history from Alpaca, computes dollar and
percentage P&L, formats a Discord message, and sends it via notify().

Jobs are registered in scheduler.py and fire at 4pm ET.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, date as _date
from typing import Optional

import pytz

from app.chart import generate_equity_chart
from app.investors import compute_breakdown, format_discord_message, load_investors
from app.notifications import notify, notify_investors, notify_with_chart
import yfinance as yf

from app.trading.alpaca_client import get_latest_price, get_portfolio_history, get_next_trading_day

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


def compute_spy_pct(period: str) -> Optional[float]:
    """Fetch SPY % return for the given period using yfinance (free, no subscription).

    Args:
        period: "1d" for daily, "5d" for weekly.

    Returns:
        % return as float, or None if data unavailable.
    """
    try:
        interval = "1m" if period == "1d" else "1d"
        hist = yf.Ticker("SPY").history(period=period, interval=interval)
        if hist.empty:
            return None
        open_price = float(hist["Open"].iloc[0])
        close_price = float(hist["Close"].iloc[-1])
        if not open_price:
            return None
        return (close_price - open_price) / open_price * 100
    except Exception as exc:
        log.warning("yfinance SPY fetch failed: %s", exc)
        return None


def fetch_spy_history(start_date: _date, end_date: _date):
    """Fetch SPY price history between two dates for chart generation.

    Returns a yfinance DataFrame with a "Close" column, or None on failure.
    """
    try:
        hist = yf.Ticker("SPY").history(start=start_date, end=end_date)
        if hist.empty:
            return None
        return hist
    except Exception as exc:
        log.warning("yfinance SPY history fetch failed: %s", exc)
        return None


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

        spy_pct = compute_spy_pct("1d")

        msg = _format_message(result, "Daily P&L", date_str, spy_pct=spy_pct)
        await notify(msg)
        log.info("Daily P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Daily P&L report failed: %s", exc)
        await notify(f"\u26a0\ufe0f Daily P&L report failed: {exc}")


async def send_weekly_report() -> None:
    """Fetch weekly portfolio history and post P&L + chart to Discord."""
    now = datetime.now(ET)
    monday = now - timedelta(days=now.weekday())
    date_str = f"Week of {monday.strftime('%b')} {monday.day}\u2013{now.day}, {now.year}"
    chart_title = f"Weekly Performance: {monday.strftime('%b %d')}\u2013{now.strftime('%b %d, %Y')}"
    try:
        history = get_portfolio_history(period="1W", timeframe="1D")
        result = _compute_pnl(history, "weekly")
        spy_pct = compute_spy_pct("5d")
        msg = _format_message(result, "Weekly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            start_date = datetime.fromtimestamp(history.timestamp[0], tz=ET).date()
            end_date = now.date() + timedelta(days=1)
            spy_df = fetch_spy_history(start_date, end_date)
            if spy_df is not None:
                chart_bytes = generate_equity_chart(
                    history.equity, history.timestamp, spy_df, chart_title
                )
        except Exception as exc:
            log.warning("Weekly chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("Weekly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Weekly P&L report failed: %s", exc)
        await notify(f"\u26a0\ufe0f Weekly P&L report failed: {exc}")


async def send_monthly_report() -> None:
    """Fetch monthly portfolio history and post P&L + chart to Discord."""
    now = datetime.now(ET)
    date_str = f"Month of {now.strftime('%B %Y')}"
    chart_title = f"Monthly Performance: {now.strftime('%B %Y')}"
    try:
        history = get_portfolio_history(period="1M", timeframe="1D")
        result = _compute_pnl(history, "monthly")
        spy_pct = compute_spy_pct("1mo")
        msg = _format_message(result, "Monthly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            start_date = datetime.fromtimestamp(history.timestamp[0], tz=ET).date()
            end_date = now.date() + timedelta(days=1)
            spy_df = fetch_spy_history(start_date, end_date)
            if spy_df is not None:
                chart_bytes = generate_equity_chart(
                    history.equity, history.timestamp, spy_df, chart_title
                )
        except Exception as exc:
            log.warning("Monthly chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("Monthly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Monthly P&L report failed: %s", exc)
        await notify(f"⚠️ Monthly P&L report failed: {exc}")


async def send_yearly_report() -> None:
    """Fetch trailing 12-month (1 year) portfolio history and post P&L + chart to Discord."""
    now = datetime.now(ET)
    date_str = f"Year {now.year}"
    chart_title = f"1-Year Performance through {now.strftime('%b %d, %Y')}"
    try:
        history = get_portfolio_history(period="1A", timeframe="1D")
        result = _compute_pnl(history, "yearly")
        spy_pct = compute_spy_pct("1y")
        msg = _format_message(result, "Yearly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            start_date = datetime.fromtimestamp(history.timestamp[0], tz=ET).date()
            end_date = now.date() + timedelta(days=1)
            spy_df = fetch_spy_history(start_date, end_date)
            if spy_df is not None:
                chart_bytes = generate_equity_chart(
                    history.equity, history.timestamp, spy_df, chart_title
                )
        except Exception as exc:
            log.warning("Yearly chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("Yearly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Yearly P&L report failed: %s", exc)
        await notify(f"⚠️ Yearly P&L report failed: {exc}")


async def send_ytd_report() -> None:
    """Fetch year-to-date portfolio history (Jan 1 to today) and post P&L to Discord."""
    now = datetime.now(ET)
    today = now.date()
    jan1 = today.replace(month=1, day=1)
    days = max((today - jan1).days, 1)
    date_str = f"YTD Jan 1–{now.strftime('%b')} {now.day}, {now.year}"
    try:
        history = get_portfolio_history(period=f"{days}D", timeframe="1D")
        result = _compute_pnl(history, "ytd")
        spy_pct = compute_spy_pct("ytd")
        msg = _format_message(result, "YTD P&L", date_str, spy_pct=spy_pct)
        await notify(msg)
        log.info("YTD P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("YTD P&L report failed: %s", exc)
        await notify(f"⚠️ YTD P&L report failed: {exc}")


async def send_alltime_report() -> None:
    """Fetch all-time portfolio history and post P&L + chart to Discord."""
    now = datetime.now(ET)
    try:
        history = get_portfolio_history(period="all", timeframe="1D")

        # Find first non-zero equity to skip pre-portfolio zeros
        start_idx = next(
            (i for i, eq in enumerate(history.equity) if eq and eq > 0),
            0,
        )
        open_eq = history.equity[start_idx]
        close_eq = history.equity[-1]
        dollar = close_eq - open_eq
        pct = (dollar / open_eq * 100) if open_eq else 0.0
        result = PnLResult(period="alltime", close_equity=close_eq, dollar_pnl=dollar, pct_pnl=pct)

        start_dt = None
        start_str = "inception"
        if start_idx < len(history.timestamp):
            start_dt = datetime.fromtimestamp(history.timestamp[start_idx], tz=ET)
            start_str = start_dt.strftime(f"%b {start_dt.day}, %Y")
        date_str = f"All Time since {start_str}"
        chart_title = f"All-Time Performance since {start_str}"

        # Fetch SPY over same date range as portfolio
        spy_pct: Optional[float] = None
        spy_df = None
        if start_dt is not None:
            spy_df = fetch_spy_history(start_dt.date(), now.date() + timedelta(days=1))
            if spy_df is not None and not spy_df.empty:
                try:
                    spy_open = float(spy_df["Open"].iloc[0])
                    spy_close = float(spy_df["Close"].iloc[-1])
                    if spy_open:
                        spy_pct = (spy_close - spy_open) / spy_open * 100
                except Exception as exc:
                    log.warning("SPY all-time pct calc failed: %s", exc)

        msg = _format_message(result, "All-Time P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            if spy_df is not None:
                chart_bytes = generate_equity_chart(
                    history.equity[start_idx:], history.timestamp[start_idx:],
                    spy_df, chart_title
                )
        except Exception as exc:
            log.warning("All-time chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("All-time P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("All-time P&L report failed: %s", exc)
        await notify(f"⚠️ All-time P&L report failed: {exc}")


async def check_period_reports() -> None:
    """Fire monthly/yearly reports when today is the last trading day of the period.

    Called Mon-Fri at 4:05 PM ET by APScheduler. Uses get_next_trading_day()
    to detect month/year boundaries correctly, including market holidays.
    """
    today = datetime.now(ET).date()
    try:
        next_trading = get_next_trading_day()
    except Exception as exc:
        log.warning("Could not fetch next trading day for period check: %s", exc)
        return
    if next_trading.month != today.month:
        await send_monthly_report()
    if next_trading.year != today.year:
        await send_yearly_report()


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
