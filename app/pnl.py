"""
pnl.py — Daily and weekly P&L calculation and Discord reporting.

Fetches portfolio equity history from Alpaca, computes dollar and
percentage P&L, formats a Discord message, and sends it via notify().

Jobs are registered in scheduler.py and fire at 4pm ET.
"""

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, date as _date
from typing import Optional

import pytz

from app.chart import generate_equity_chart
from app.investors import compute_breakdown, format_discord_message, load_investors
from app.notifications import notify, notify_investors, notify_with_chart
import yfinance as yf

from app.trading.alpaca_client import get_account, get_latest_price, get_portfolio_history, get_next_trading_day

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

_yf_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="yfinance")
_YF_TIMEOUT = 15.0


def _yf_fetch(fn):
    """Run a yfinance call with a timeout to prevent indefinite hangs."""
    future = _yf_executor.submit(fn)
    try:
        return future.result(timeout=_YF_TIMEOUT)
    except concurrent.futures.TimeoutError:
        log.warning("yfinance fetch timed out after %ss", _YF_TIMEOUT)
        return None


@dataclass
class PnLResult:
    period: str        # "daily" or "weekly"
    close_equity: float
    dollar_pnl: float
    pct_pnl: float


def _first_nonzero_idx(equity) -> int:
    """Return index of first non-zero equity value, or 0 if none found."""
    return next((i for i, eq in enumerate(equity) if eq and eq > 0), 0)


def _compute_pnl(history, period: str, start_idx: int = 0) -> PnLResult:
    """Compute P&L from a PortfolioHistory object.

    Uses first equity value as open and last as close.
    """
    if not history.equity or start_idx >= len(history.equity):
        raise ValueError(f"No equity data available for {period} report")
    open_eq = history.equity[start_idx]
    close_eq = next((eq for eq in reversed(history.equity) if eq is not None), None)
    if close_eq is None:
        raise ValueError(f"No valid close equity for {period} report")
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
        hist = _yf_fetch(lambda: yf.Ticker("SPY").history(period=period, interval=interval))
        if hist is None or hist.empty:
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
        hist = _yf_fetch(lambda: yf.Ticker("SPY").history(start=start_date, end=end_date))
        if hist is None or hist.empty:
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
        relative_rounded = round(relative, 2)
        if relative_rounded > 0:
            comparison = f"OUTPERFORM by {relative_rounded:.2f}%"
        elif relative_rounded < 0:
            comparison = f"UNDERPERFORM by {abs(relative_rounded):.2f}%"
        else:
            comparison = "IN LINE"
        msg += f"\nS&P 500: {spy_sign}{spy_pct:.2f}%"
        msg += f"\n{comparison}"

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
        start_idx = _first_nonzero_idx(history.equity)

        # Build equity/timestamps first so P&L and chart share identical data
        equity = list(history.equity[start_idx:])
        timestamps = list(history.timestamp[start_idx:])
        last_date = datetime.fromtimestamp(timestamps[-1], tz=ET).date()
        if last_date < now.date():
            try:
                account = get_account()
                equity.append(float(account.equity))
                timestamps.append(int(now.timestamp()))
            except Exception as exc:
                log.warning("Could not fetch current equity for weekly report: %s", exc)

        # P&L from the same equity list the chart uses
        open_eq = equity[0] if equity and equity[0] else 1.0
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for weekly report")
        dollar_pnl = close_eq - open_eq
        pct_pnl = (dollar_pnl / open_eq * 100) if open_eq else 0.0
        result = PnLResult(period="weekly", close_equity=close_eq, dollar_pnl=dollar_pnl, pct_pnl=pct_pnl)

        # SPY from same date range and same Close.iloc[0] baseline as chart
        start_date = datetime.fromtimestamp(timestamps[0], tz=ET).date()
        end_date = now.date() + timedelta(days=1)
        spy_df = fetch_spy_history(start_date, end_date)

        spy_pct: Optional[float] = None
        if spy_df is not None and not spy_df.empty and "Close" in spy_df.columns:
            spy_open = float(spy_df["Close"].iloc[0])
            spy_close = float(spy_df["Close"].iloc[-1])
            if spy_open:
                spy_pct = (spy_close - spy_open) / spy_open * 100

        msg = _format_message(result, "Weekly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            if spy_df is not None:
                loop = asyncio.get_running_loop()
                chart_bytes = await loop.run_in_executor(
                    None, generate_equity_chart,
                    equity, timestamps, spy_df, chart_title
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
        start_idx = _first_nonzero_idx(history.equity)

        equity = list(history.equity[start_idx:])
        timestamps = list(history.timestamp[start_idx:])
        last_date = datetime.fromtimestamp(timestamps[-1], tz=ET).date()
        if last_date < now.date():
            try:
                account = get_account()
                equity.append(float(account.equity))
                timestamps.append(int(now.timestamp()))
            except Exception as exc:
                log.warning("Could not fetch current equity for monthly report: %s", exc)

        open_eq = equity[0] if equity and equity[0] else 1.0
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for monthly report")
        dollar_pnl = close_eq - open_eq
        pct_pnl = (dollar_pnl / open_eq * 100) if open_eq else 0.0
        result = PnLResult(period="monthly", close_equity=close_eq, dollar_pnl=dollar_pnl, pct_pnl=pct_pnl)

        start_date = datetime.fromtimestamp(timestamps[0], tz=ET).date()
        end_date = now.date() + timedelta(days=1)
        spy_df = fetch_spy_history(start_date, end_date)

        spy_pct: Optional[float] = None
        if spy_df is not None and not spy_df.empty and "Close" in spy_df.columns:
            spy_open = float(spy_df["Close"].iloc[0])
            spy_close = float(spy_df["Close"].iloc[-1])
            if spy_open:
                spy_pct = (spy_close - spy_open) / spy_open * 100

        msg = _format_message(result, "Monthly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            if spy_df is not None:
                loop = asyncio.get_running_loop()
                chart_bytes = await loop.run_in_executor(
                    None, generate_equity_chart,
                    equity, timestamps, spy_df, chart_title
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
        start_idx = _first_nonzero_idx(history.equity)

        equity = list(history.equity[start_idx:])
        timestamps = list(history.timestamp[start_idx:])
        last_date = datetime.fromtimestamp(timestamps[-1], tz=ET).date()
        if last_date < now.date():
            try:
                account = get_account()
                equity.append(float(account.equity))
                timestamps.append(int(now.timestamp()))
            except Exception as exc:
                log.warning("Could not fetch current equity for yearly report: %s", exc)

        open_eq = equity[0] if equity and equity[0] else 1.0
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for yearly report")
        dollar_pnl = close_eq - open_eq
        pct_pnl = (dollar_pnl / open_eq * 100) if open_eq else 0.0
        result = PnLResult(period="yearly", close_equity=close_eq, dollar_pnl=dollar_pnl, pct_pnl=pct_pnl)

        start_date = datetime.fromtimestamp(timestamps[0], tz=ET).date()
        end_date = now.date() + timedelta(days=1)
        spy_df = fetch_spy_history(start_date, end_date)

        spy_pct: Optional[float] = None
        if spy_df is not None and not spy_df.empty and "Close" in spy_df.columns:
            spy_open = float(spy_df["Close"].iloc[0])
            spy_close = float(spy_df["Close"].iloc[-1])
            if spy_open:
                spy_pct = (spy_close - spy_open) / spy_open * 100

        msg = _format_message(result, "Yearly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            if spy_df is not None:
                loop = asyncio.get_running_loop()
                chart_bytes = await loop.run_in_executor(
                    None, generate_equity_chart,
                    equity, timestamps, spy_df, chart_title
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
        start_idx = _first_nonzero_idx(history.equity)
        result = _compute_pnl(history, "ytd", start_idx)
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

        start_idx = _first_nonzero_idx(history.equity)
        if not history.equity or start_idx >= len(history.equity):
            raise ValueError("No equity data available for all-time report")

        # Build equity/timestamps and extend to today so P&L and chart share identical data
        equity = list(history.equity[start_idx:])
        timestamps = list(history.timestamp[start_idx:])
        last_date = datetime.fromtimestamp(timestamps[-1], tz=ET).date()
        if last_date < now.date():
            try:
                account = get_account()
                equity.append(float(account.equity))
                timestamps.append(int(now.timestamp()))
            except Exception as exc:
                log.warning("Could not fetch current equity for all-time report: %s", exc)

        # P&L from the same equity list the chart uses, with None guard
        open_eq = equity[0] if equity and equity[0] else 1.0
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for all-time report")
        dollar = close_eq - open_eq
        pct = (dollar / open_eq * 100) if open_eq else 0.0
        result = PnLResult(period="alltime", close_equity=close_eq, dollar_pnl=dollar, pct_pnl=pct)

        start_dt = datetime.fromtimestamp(timestamps[0], tz=ET)
        start_str = start_dt.strftime(f"%b {start_dt.day}, %Y")
        date_str = f"All Time since {start_str}"
        chart_title = f"All-Time Performance since {start_str}"

        # SPY from same date range and same Close.iloc[0] baseline as chart
        spy_df = fetch_spy_history(start_dt.date(), now.date() + timedelta(days=1))

        spy_pct: Optional[float] = None
        if spy_df is not None and not spy_df.empty and "Close" in spy_df.columns:
            spy_open = float(spy_df["Close"].iloc[0])
            spy_close = float(spy_df["Close"].iloc[-1])
            if spy_open:
                spy_pct = (spy_close - spy_open) / spy_open * 100

        msg = _format_message(result, "All-Time P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            if spy_df is not None:
                loop = asyncio.get_running_loop()
                chart_bytes = await loop.run_in_executor(
                    None, generate_equity_chart,
                    equity, timestamps, spy_df, chart_title
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
    try:
        breakdown = compute_breakdown(investors, spy_price)
        message = format_discord_message(breakdown, date_str)
        await notify_investors(message)
        log.info("Investor report sent for %s", date_str)
    except Exception as exc:
        log.error("Investor report failed: %s", exc)
        await notify_investors(f"⚠️ Investor report failed: {exc}")
