"""
scheduler.py — APScheduler configuration and P&L job registration.

The `scheduler` singleton is started/stopped in main.py's lifespan.
Call setup_jobs() once at startup to register the cron triggers.
"""

import asyncio
import logging
from datetime import datetime, timedelta, date as _date

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.pnl import send_daily_report, send_investor_report, send_weekly_report, check_period_reports
from app.rh_equity_history import get_snapshots
from app.rh_keep_alive_state import get_next_run_ts, record_run
from app.rh_pnl import record_rh_equity_snapshot, send_rh_report
from app.pending_withdrawals import load_pending_withdrawals
from app.trading.alpaca_client import was_market_open_today, get_client

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


# Singleton — imported and started in main.py lifespan.
scheduler = AsyncIOScheduler(timezone=ET)


async def _check_rh_period_reports() -> None:
    """Fire RH monthly/yearly reports on the last trading day of the period."""
    from app.trading.alpaca_client import get_next_trading_day
    today = datetime.now(ET).date()
    try:
        next_trading = get_next_trading_day()
    except Exception as exc:
        log.warning("Could not fetch next trading day for RH period check: %s", exc)
        return
    if next_trading.month != today.month:
        await send_rh_report("monthly")
    if next_trading.year != today.year:
        await send_rh_report("1year")


async def _weekday_jobs() -> None:
    """Run Mon–Thu reports in parallel: Alpaca + RH daily, investor breakdown, period checks."""
    if not was_market_open_today():
        log.info("_weekday_jobs: market holiday — skipping all reports")
        return
    # Record today's RH-equity/SPY-price snapshot first so send_rh_report
    # below can use it as "today" — keeps the pair in sync, no drift.
    await record_rh_equity_snapshot()
    await asyncio.gather(
        send_daily_report(),
        send_investor_report(),
        check_period_reports(),
        send_rh_report("daily"),
        _check_rh_period_reports(),
        return_exceptions=True,
    )


async def _portfolio_snapshot() -> None:
    from app.portfolio_report import send_portfolio_snapshot
    await send_portfolio_snapshot()


async def _friday_jobs() -> None:
    """Run Friday reports in parallel: Alpaca + RH daily/weekly, investor breakdown, period checks, portfolio snapshot."""
    if not was_market_open_today():
        log.info("_friday_jobs: market holiday — skipping all reports")
        return
    # Record today's RH-equity/SPY-price snapshot first so send_rh_report
    # below can use it as "today" — keeps the pair in sync, no drift.
    await record_rh_equity_snapshot()
    await asyncio.gather(
        send_daily_report(),
        send_weekly_report(),
        send_investor_report(),
        check_period_reports(),
        send_rh_report("daily"),
        send_rh_report("weekly"),
        _check_rh_period_reports(),
        _portfolio_snapshot(),
        return_exceptions=True,
    )


async def _robinhood_keep_alive() -> None:
    """Refresh the Robinhood session at a random time between 1–5 AM ET, every 1–2 days.

    Cron fires every 15 minutes inside that window. Fires only when
    now >= next_run_ts so the exact refresh time is unpredictable.
    If the state file is absent (first run or /data wiped), fires immediately.
    """
    if not settings.rh_enabled:
        return
    from app.trading.robinhood_client import rh_client
    now_ts = int(datetime.now(ET).timestamp())
    next_run = get_next_run_ts()
    if next_run is not None and now_ts < next_run:
        return
    await rh_client.keep_alive()
    record_run(now_ts)


async def _quarterly_tax_report() -> None:
    """Post Alpaca + RH tax summaries to their respective channels.

    Fires on the first trading day of Jan/Apr/Jul/Oct (cron covers days 1–3
    to handle cases where the 1st is a holiday or weekend).

    Jan: reports the just-completed year (year - 1).
    Apr/Jul/Oct: reports the current year YTD.
    """
    # Skip if the market is closed today (e.g. Jan 1 is always a holiday)
    if not was_market_open_today():
        log.info("_quarterly_tax_report: market holiday — skipping")
        return

    # If the cron fired on day 2 or 3, skip if day 1 was already a trading day
    # (prevents double-firing when the 1st is open and the 2nd/3rd also trigger)
    today = _date.today()
    first_of_month = today.replace(day=1)
    if first_of_month < today:
        try:
            from alpaca.trading.requests import GetCalendarRequest
            prior = get_client().get_calendar(
                GetCalendarRequest(start=first_of_month, end=today - timedelta(days=1))
            )
            if prior:
                log.info("_quarterly_tax_report: not first trading day of quarter — skipping")
                return
        except Exception as exc:
            log.warning("Could not verify first trading day for tax report: %s", exc)

    from app.tax import send_alpaca_tax_report, send_rh_tax_report
    now = datetime.now(ET)
    year = now.year - 1 if now.month == 1 else now.year
    results = await asyncio.gather(
        send_alpaca_tax_report(year),
        send_rh_tax_report(year),
        return_exceptions=True,
    )
    for name, result in zip(("alpaca_tax", "rh_tax"), results):
        if isinstance(result, Exception):
            log.error("Quarterly tax report failed for %s: %s", name, result)


async def _claude_monthly_rebalance() -> None:
    from app.claude_manager import run_monthly_rebalance
    await run_monthly_rebalance()


async def _nightly_backup() -> None:
    from app.backup import push_backup
    await push_backup()


def setup_jobs() -> None:
    """Register cron jobs — two parallel bundles replacing five staggered jobs."""
    scheduler.add_job(
        _weekday_jobs,
        CronTrigger(day_of_week="mon-thu", hour=16, minute=0, timezone=ET),
        id="weekday_jobs",
        replace_existing=True,
    )
    scheduler.add_job(
        _friday_jobs,
        CronTrigger(day_of_week="fri", hour=16, minute=0, timezone=ET),
        id="friday_jobs",
        replace_existing=True,
    )
    scheduler.add_job(
        _robinhood_keep_alive,
        CronTrigger(hour="1,2,3,4", minute="0,15,30,45", timezone=ET),
        id="robinhood_keep_alive",
        replace_existing=True,
    )
    scheduler.add_job(
        _quarterly_tax_report,
        CronTrigger(month="1,4,7,10", day="1-3", hour=8, minute=0, timezone=ET),
        id="quarterly_tax_report",
        replace_existing=True,
    )
    scheduler.add_job(
        _claude_monthly_rebalance,
        CronTrigger(day=1, hour=9, minute=35, timezone=ET),
        id="claude_monthly_rebalance",
        replace_existing=True,
    )
    scheduler.add_job(
        _nightly_backup,
        CronTrigger(hour=0, minute=0, timezone=ET),
        id="nightly_backup",
        replace_existing=True,
    )
    log.info(
        "Scheduler jobs registered: weekday_jobs, friday_jobs (Alpaca+RH), "
        "robinhood_keep_alive (daily check, runs every ~3 days), "
        "quarterly_tax_report (Jan/Apr/Jul/Oct 1), "
        "claude_monthly_rebalance (1st of each month 9:35 AM ET), "
        "nightly_backup (daily midnight ET)"
    )


async def catch_up_equity_snapshot() -> None:
    """Backfill today's RH equity snapshot if a restart caused the 4 PM ET
    scheduler tick to be missed.

    record_rh_equity_snapshot() only normally fires via the weekday_jobs/
    friday_jobs CronTrigger at 16:00 ET. APScheduler computes each trigger's
    next fire time strictly forward from scheduler start — if the process
    restarts (e.g. a Render auto-deploy) spanning that tick, that day's
    snapshot is silently and permanently skipped, with nothing left to retry.
    Called once at startup: if it's already past 4 PM ET on a trading day
    and today isn't recorded yet, record it now.
    """
    now = datetime.now(ET)
    if now.hour < 16:
        return
    if not was_market_open_today():
        return
    today_str = now.date().isoformat()
    if any(s["date"] == today_str for s in get_snapshots()):
        return
    log.info("Backfilling missed RH equity snapshot for %s", today_str)
    await record_rh_equity_snapshot()


def reschedule_pending_orders() -> None:
    from app.pending_orders import load_pending_orders
    from app.trade_notifier import notify_pending_order_fill
    from app.rh_trade_notifier import notify_rh_pending_fill

    orders = load_pending_orders()
    if not orders:
        return

    now = datetime.now(ET)
    for entry in orders:
        order_id = entry["order_id"]
        broker = entry.get("broker", "alpaca")
        try:
            run_dt = datetime.fromisoformat(entry["run_at"])
            if run_dt.tzinfo is None:
                run_dt = ET.localize(run_dt)
        except Exception:
            run_dt = now

        effective_run = run_dt if run_dt > now else now

        if broker == "claude_sell":
            from app.claude_manager import notify_claude_pending_sell_fill
            scheduler.add_job(
                notify_claude_pending_sell_fill,
                "date",
                run_date=effective_run,
                args=[order_id, entry["ticker"], entry.get("avg_entry_price", 0.0),
                      entry.get("qty", 0.0), entry.get("source", "manager")],
                id=f"pending_{order_id}",
                replace_existing=True,
            )
        elif broker == "rh":
            scheduler.add_job(
                notify_rh_pending_fill,
                "date",
                run_date=effective_run,
                args=[order_id, entry["ticker"], entry["action"],
                      entry.get("side", "buy"), entry.get("qty"),
                      entry.get("alert_price"), entry.get("avg_buy_price")],
                id=f"pending_{order_id}",
                replace_existing=True,
            )
        else:
            scheduler.add_job(
                notify_pending_order_fill,
                "date",
                run_date=effective_run,
                args=[order_id, entry["ticker"], entry["action"],
                      entry.get("alert_price"), entry.get("avg_entry_price")],
                id=f"pending_{order_id}",
                replace_existing=True,
            )
        log.info("Rescheduled pending %s order %s for %s", broker, order_id, effective_run)


def reschedule_pending_withdrawals() -> None:
    from app.withdrawal_execution import execute_pending_withdrawal

    records = load_pending_withdrawals()
    if not records:
        return

    now = datetime.now(ET)
    for record in records:
        withdrawal_id = record["id"]
        try:
            run_dt = datetime.fromisoformat(record["run_at"])
            if run_dt.tzinfo is None:
                run_dt = ET.localize(run_dt)
        except Exception:
            run_dt = now

        effective_run = run_dt if run_dt > now else now

        scheduler.add_job(
            execute_pending_withdrawal,
            "date",
            run_date=effective_run,
            args=[withdrawal_id],
            id=f"withdrawal_{withdrawal_id}",
            replace_existing=True,
        )
        log.info("Rescheduled pending withdrawal %s for %s", withdrawal_id, effective_run)
