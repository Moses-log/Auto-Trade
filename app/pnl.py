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

from app.chart import generate_equity_chart, generate_investor_pie_chart
from app.alpaca_hf_record import contribution_total
from app.investors import compute_breakdown, format_discord_message, load_investors
from app.notifications import notify, notify_investors, notify_investors_with_chart, notify_with_chart
import yfinance as yf

from app.trading.alpaca_client import (
    get_account, get_alpaca_deposit_events, get_deposit_events_from_orders,
    get_latest_price, get_portfolio_history, get_next_trading_day,
)

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# Date the fund started trading — used for the "Since Inception" report.
FUND_INCEPTION_DATE = _date(2026, 4, 27)

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


def _fund_inception_idx(timestamps, equity) -> int:
    """Return first index at or after FUND_INCEPTION_DATE with non-zero equity.

    Prevents pre-fund Alpaca account history from contaminating P&L baselines.
    Falls back to _first_nonzero_idx if no inception-bounded sample exists.
    """
    inception_epoch = ET.localize(
        datetime.combine(FUND_INCEPTION_DATE, datetime.min.time())
    ).timestamp()
    for i, (ts, eq) in enumerate(zip(timestamps, equity)):
        if ts is not None and ts >= inception_epoch and eq and eq > 0:
            return i
    return _first_nonzero_idx(equity)


def _deposit_events() -> list[tuple[str, float]]:
    """External cash-in events for the Alpaca fund P&L deposit adjustment.

    Deposits are sourced from Alpaca **orders** (a manual dollar/notional BUY),
    which carry the exact amount and the exact date the cash was invested — i.e.
    the equity-jump bar — with no settlement-activity lag and no ledger
    command-date lag. Falls back to the account-activities API only if the
    order-based source returns nothing (e.g. a permissions/API hiccup)."""
    try:
        orders = get_deposit_events_from_orders()
        if orders:
            return orders
    except Exception as exc:
        log.warning("Order-based deposit source failed: %s", exc)
    return get_alpaca_deposit_events()


_DEPOSIT_ALIGN_WINDOW_DAYS = 3  # a deposit's date may sit within a few bars of its jump


def align_deposits_to_bars(
    equity: list,
    bar_dates: list,
    deposit_events: list[tuple[str, float]],
) -> dict:
    """Map each deposit to the equity-bar *index* where its cash actually lands.

    For each deposit we look only at bars AFTER the first bar (index >= 1) and
    within a few days of the recorded date, and take the positive bar-over-bar
    jump closest in size to the deposit amount — the bar where the cash visibly
    arrives. A deposit is subtracted ONLY if such a jump exists and is plausibly
    that deposit (within 50% of its size). Deposits that predate the window (the
    fund's baseline capital) leave no in-window jump and are therefore never
    stripped — this is the rule whose absence cratered the chart. Returns
    {bar_index: total_amount}.

    `bar_dates` is a list of `date` (or None) parallel to `equity`. Aligning on
    the jump rather than on the recorded date is what makes this correct whether
    the cash lands the same day, the next trading day, or a weekend-dated
    deposit lands on the following Monday. Shared by the Alpaca P&L charts and
    the Kimi Manager track record — keep it that way; a second copy of this rule
    is how the transient-spike bug came back."""
    from collections import defaultdict
    by_index: dict = defaultdict(float)
    used: set = set()
    for date_str, amt in sorted(deposit_events):
        try:
            d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        best_i, best_err = None, None
        for i in range(1, len(equity)):
            if i in used or bar_dates[i] is None:
                continue
            if abs((bar_dates[i] - d).days) > _DEPOSIT_ALIGN_WINDOW_DAYS:
                continue
            if equity[i] is None or equity[i - 1] is None:
                continue
            jump = equity[i] - equity[i - 1]
            if jump <= 0:
                continue
            err = abs(jump - amt)
            if best_err is None or err < best_err:
                best_err, best_i = err, i
        # Only strip when a real in-window jump plausibly IS this deposit. No jump
        # -> baseline capital or masked -> skip (never a blind subtraction).
        if best_i is not None and best_err <= amt * 0.5:
            by_index[best_i] += amt
            used.add(best_i)
    return by_index


def _align_deposits_to_equity(
    equity: list,
    timestamps: list,
    deposit_events: list[tuple[str, float]],
) -> dict:
    """align_deposits_to_bars for an Alpaca series keyed by unix timestamps."""
    bar_dates = [
        datetime.fromtimestamp(ts, tz=ET).date() if ts is not None else None
        for ts in timestamps
    ]
    return align_deposits_to_bars(equity, bar_dates, deposit_events)


def deposit_adjusted_equity(
    equity: list,
    timestamps: list,
    deposit_events: list[tuple[str, float]],
) -> list:
    """Return the equity series with external deposits subtracted, so the curve
    reflects only trading performance.

    Explicit deposit_events are aligned to the equity bar where the cash actually
    appears (see _align_deposits_to_equity) and only in-window jumps are stripped
    — baseline capital is never touched. With no events, falls back to
    auto-detection: any bar-over-bar increase >20% is treated as a deposit.

    Guardrail: if the result would ever go negative (an over-subtraction bug),
    the adjustment is abandoned and the raw equity is returned, so a broken
    deposit calc can never render a catastrophic crater to viewers.
    """
    from collections import defaultdict
    if deposit_events:
        by_index = _align_deposits_to_equity(equity, timestamps, deposit_events)
    else:
        by_index = defaultdict(float)
        for i in range(1, len(equity)):
            prev_eq, curr_eq = equity[i - 1], equity[i]
            if prev_eq is None or curr_eq is None or prev_eq <= 0:
                continue
            if (curr_eq - prev_eq) / prev_eq > 0.20:
                by_index[i] += curr_eq - prev_eq

    result = []
    cumulative = 0.0
    for i, eq in enumerate(equity):
        cumulative += by_index.get(i, 0.0)
        result.append((eq - cumulative) if eq is not None else None)

    if any(a is not None and a < 0 for a in result):
        log.warning(
            "Deposit adjustment produced negative equity — abandoning adjustment "
            "and returning raw equity (guardrail against a crater)."
        )
        return list(equity)
    return result


def _compute_pnl(history, period: str, start_idx: int = 0) -> PnLResult:
    """Compute deposit-adjusted P&L from a PortfolioHistory object."""
    if not history.equity or start_idx >= len(history.equity):
        raise ValueError(f"No equity data available for {period} report")
    equity = list(history.equity[start_idx:])
    timestamps = list(history.timestamp[start_idx:])
    raw_close = next((eq for eq in reversed(equity) if eq is not None), None)
    if raw_close is None:
        raise ValueError(f"No valid close equity for {period} report")
    adj = deposit_adjusted_equity(equity, timestamps, _deposit_events())
    open_adj = next((eq for eq in adj if eq), None) or equity[0]
    close_adj = next((eq for eq in reversed(adj) if eq is not None), None) or raw_close
    dollar = close_adj - open_adj
    pct = (dollar / open_adj * 100) if open_adj else 0.0
    return PnLResult(period=period, close_equity=raw_close, dollar_pnl=dollar, pct_pnl=pct)


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


def fetch_spy_close_price() -> Optional[float]:
    """Fetch SPY's most recent close price via yfinance.

    Paired with RH's equity snapshot at the same scheduler tick so the two
    numbers are captured at the same instant and can't drift apart.
    """
    try:
        hist = _yf_fetch(lambda: yf.Ticker("SPY").history(period="1d"))
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as exc:
        log.warning("yfinance SPY close price fetch failed: %s", exc)
        return None


def format_spy_comparison_lines(pct_pnl: float, spy_pct: float) -> list[str]:
    """Return Discord message lines comparing a portfolio's % return to SPY's.

    Shared by Alpaca's _format_message and the Robinhood P&L report so both
    brokers' SPY comparisons read identically.
    """
    spy_sign = "+" if spy_pct >= 0 else ""
    relative_rounded = round(pct_pnl - spy_pct, 2)
    if relative_rounded > 0:
        comparison = f"OUTPERFORM by {relative_rounded:.2f}%"
    elif relative_rounded < 0:
        comparison = f"UNDERPERFORM by {abs(relative_rounded):.2f}%"
    else:
        comparison = "IN LINE"
    return [f"S&P 500: {spy_sign}{spy_pct:.2f}%", comparison]


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
        msg += "\n" + "\n".join(format_spy_comparison_lines(result.pct_pnl, spy_pct))

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
    if monday.month == now.month:
        date_str = f"Week of {monday.strftime('%b')} {monday.day}\u2013{now.day}, {now.year}"
    else:
        date_str = f"Week of {monday.strftime('%b')} {monday.day} \u2013 {now.strftime('%b')} {now.day}, {now.year}"
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

        # Deposit-adjusted P&L — strips out capital injections
        open_eq = next((eq for eq in equity if eq), None)
        if open_eq is None:
            raise ValueError("No valid open equity")
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for weekly report")
        _devts = _deposit_events()
        _adj = deposit_adjusted_equity(equity, timestamps, _devts)
        _adj_open = next((eq for eq in _adj if eq), None) or open_eq
        _adj_close = next((eq for eq in reversed(_adj) if eq is not None), None) or close_eq
        dollar_pnl = _adj_close - _adj_open
        pct_pnl = (dollar_pnl / _adj_open * 100) if _adj_open else 0.0
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
                    _adj, timestamps, spy_df, chart_title
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

        open_eq = next((eq for eq in equity if eq), None)
        if open_eq is None:
            raise ValueError("No valid open equity")
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for monthly report")
        _devts = _deposit_events()
        _adj = deposit_adjusted_equity(equity, timestamps, _devts)
        _adj_open = next((eq for eq in _adj if eq), None) or open_eq
        _adj_close = next((eq for eq in reversed(_adj) if eq is not None), None) or close_eq
        dollar_pnl = _adj_close - _adj_open
        pct_pnl = (dollar_pnl / _adj_open * 100) if _adj_open else 0.0
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
                    _adj, timestamps, spy_df, chart_title
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

        open_eq = next((eq for eq in equity if eq), None)
        if open_eq is None:
            raise ValueError("No valid open equity")
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for yearly report")
        _devts = _deposit_events()
        _adj = deposit_adjusted_equity(equity, timestamps, _devts)
        _adj_open = next((eq for eq in _adj if eq), None) or open_eq
        _adj_close = next((eq for eq in reversed(_adj) if eq is not None), None) or close_eq
        dollar_pnl = _adj_close - _adj_open
        pct_pnl = (dollar_pnl / _adj_open * 100) if _adj_open else 0.0
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
                    _adj, timestamps, spy_df, chart_title
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
    """Fetch year-to-date portfolio history (Jan 1 to today) and post P&L + chart to Discord."""
    now = datetime.now(ET)
    today = now.date()
    jan1 = today.replace(month=1, day=1)
    # If fund started mid-year, anchor YTD to inception so pre-fund history is excluded
    effective_start = FUND_INCEPTION_DATE if FUND_INCEPTION_DATE > jan1 else jan1
    days = max((today - effective_start).days, 1)
    start_label = effective_start.strftime(f"%b {effective_start.day}")
    date_str = f"YTD {start_label}–{now.strftime('%b')} {now.day}, {now.year}"
    chart_title = f"YTD Performance: {start_label}–{now.strftime('%b %d, %Y')}"
    try:
        history = get_portfolio_history(period=f"{days}D", timeframe="1D")
        start_idx = _fund_inception_idx(history.timestamp, history.equity)

        equity = list(history.equity[start_idx:])
        timestamps = list(history.timestamp[start_idx:])
        last_date = datetime.fromtimestamp(timestamps[-1], tz=ET).date()
        if last_date < today:
            try:
                account = get_account()
                equity.append(float(account.equity))
                timestamps.append(int(now.timestamp()))
            except Exception as exc:
                log.warning("Could not fetch current equity for YTD report: %s", exc)

        open_eq = next((eq for eq in equity if eq), None)
        if open_eq is None:
            raise ValueError("No valid open equity")
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for YTD report")
        _devts = _deposit_events()
        _adj = deposit_adjusted_equity(equity, timestamps, _devts)
        _adj_open = next((eq for eq in _adj if eq), None) or open_eq
        _adj_close = next((eq for eq in reversed(_adj) if eq is not None), None) or close_eq
        dollar_pnl = _adj_close - _adj_open
        pct_pnl = (dollar_pnl / _adj_open * 100) if _adj_open else 0.0
        result = PnLResult(period="ytd", close_equity=close_eq, dollar_pnl=dollar_pnl, pct_pnl=pct_pnl)

        start_date = datetime.fromtimestamp(timestamps[0], tz=ET).date()
        spy_df = fetch_spy_history(start_date, today + timedelta(days=1))

        spy_pct: Optional[float] = None
        if spy_df is not None and not spy_df.empty and "Close" in spy_df.columns:
            spy_open = float(spy_df["Close"].iloc[0])
            spy_close = float(spy_df["Close"].iloc[-1])
            if spy_open:
                spy_pct = (spy_close - spy_open) / spy_open * 100

        msg = _format_message(result, "YTD P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            if spy_df is not None:
                loop = asyncio.get_running_loop()
                chart_bytes = await loop.run_in_executor(
                    None, generate_equity_chart,
                    _adj, timestamps, spy_df, chart_title
                )
        except Exception as exc:
            log.warning("YTD chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
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

        start_idx = _fund_inception_idx(history.timestamp, history.equity)
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

        open_eq = next((eq for eq in equity if eq), None)
        if open_eq is None:
            raise ValueError("No valid open equity")
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError("No valid close equity for all-time report")
        _devts = _deposit_events()
        _adj = deposit_adjusted_equity(equity, timestamps, _devts)
        _adj_open = next((eq for eq in _adj if eq), None) or open_eq
        _adj_close = next((eq for eq in reversed(_adj) if eq is not None), None) or close_eq
        dollar = _adj_close - _adj_open
        pct = (dollar / _adj_open * 100) if _adj_open else 0.0
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
                    _adj, timestamps, spy_df, chart_title
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


async def _send_since_date_report(start_date: _date, label: str, period_key: str) -> None:
    """Shared implementation for the "Since Inception" and "Custom Date" reports.

    Fetches portfolio history from start_date through today and posts P&L + chart.
    """
    now = datetime.now(ET)
    try:
        start_dt = ET.localize(datetime.combine(start_date, datetime.min.time()))
        # Alpaca's `period` defaults to "1M" (~22 trading days) when omitted,
        # which silently caps the result even when `start` reaches back further.
        # Pass an explicit period covering the full span so no days are dropped.
        days_span = (now.date() - start_date).days + 1
        history = get_portfolio_history(timeframe="1D", start=start_dt, period=f"{days_span}D")

        start_idx = _first_nonzero_idx(history.equity)
        if not history.equity or start_idx >= len(history.equity):
            raise ValueError(f"No equity data available for {label} report")

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
                log.warning("Could not fetch current equity for %s report: %s", label, exc)

        open_eq = next((eq for eq in equity if eq), None)
        if open_eq is None:
            raise ValueError("No valid open equity")
        close_eq = next((eq for eq in reversed(equity) if eq is not None), None)
        if close_eq is None:
            raise ValueError(f"No valid close equity for {label} report")
        _devts = _deposit_events()
        _adj = deposit_adjusted_equity(equity, timestamps, _devts)
        _adj_open = next((eq for eq in _adj if eq), None) or open_eq
        _adj_close = next((eq for eq in reversed(_adj) if eq is not None), None) or close_eq
        dollar = _adj_close - _adj_open
        pct = (dollar / _adj_open * 100) if _adj_open else 0.0
        result = PnLResult(period=period_key, close_equity=close_eq, dollar_pnl=dollar, pct_pnl=pct)

        actual_start = datetime.fromtimestamp(timestamps[0], tz=ET)
        start_str = actual_start.strftime(f"%b {actual_start.day}, %Y")
        end_str = now.strftime(f"%b {now.day}, %Y")
        date_str = f"Since {start_str}"
        chart_title = f"{label} Performance: {start_str} – {end_str}"

        # SPY from same date range and same Close.iloc[0] baseline as chart
        spy_df = fetch_spy_history(actual_start.date(), now.date() + timedelta(days=1))

        spy_pct: Optional[float] = None
        if spy_df is not None and not spy_df.empty and "Close" in spy_df.columns:
            spy_open = float(spy_df["Close"].iloc[0])
            spy_close = float(spy_df["Close"].iloc[-1])
            if spy_open:
                spy_pct = (spy_close - spy_open) / spy_open * 100

        msg = _format_message(result, f"{label} P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            if spy_df is not None:
                loop = asyncio.get_running_loop()
                chart_bytes = await loop.run_in_executor(
                    None, generate_equity_chart,
                    _adj, timestamps, spy_df, chart_title
                )
        except Exception as exc:
            log.warning("%s chart generation failed: %s", label, exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("%s P&L report sent: dollar=%.2f pct=%.2f", label, result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("%s P&L report failed: %s", label, exc)
        await notify(f"⚠️ {label} P&L report failed: {exc}")


async def send_inception_report() -> None:
    """Fetch portfolio history since fund inception and post P&L + chart to Discord."""
    await _send_since_date_report(FUND_INCEPTION_DATE, "Since Inception", "inception")


async def send_custom_report(start_date: _date) -> None:
    """Fetch portfolio history since a user-specified date and post P&L + chart to Discord."""
    await _send_since_date_report(start_date, "Custom", "custom")


async def check_period_reports() -> None:
    """Fire monthly/yearly reports when today is the last trading day of the period.

    Called Mon-Fri at 4:00 PM ET by APScheduler. Uses get_next_trading_day()
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

    try:
        account = get_account()
        real_total_equity = float(account.equity)
    except Exception as exc:
        log.warning("Could not fetch account equity; skipping investor report: %s", exc)
        return

    now = datetime.now(ET)
    date_str = now.strftime(f"%B {now.day}, %Y")
    try:
        nonspy_pnl = await contribution_total()
        breakdown = compute_breakdown(investors, spy_price, real_total_equity, nonspy_pnl=nonspy_pnl)
        message = format_discord_message(breakdown, date_str)
        chart_bytes = None
        try:
            loop = asyncio.get_running_loop()
            chart_bytes = await loop.run_in_executor(None, generate_investor_pie_chart, breakdown, date_str)
        except Exception as exc:
            log.warning("Investor pie chart generation failed: %s", exc)
        if chart_bytes:
            await notify_investors_with_chart(message, chart_bytes)
        else:
            await notify_investors(message)
        log.info("Investor report sent for %s", date_str)
    except Exception as exc:
        log.error("Investor report failed: %s", exc)
        await notify_investors(f"⚠️ Investor report failed: {exc}")
