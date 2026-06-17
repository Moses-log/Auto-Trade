"""
scheduler.py — APScheduler configuration and P&L job registration.

The `scheduler` singleton is started/stopped in main.py's lifespan.
Call setup_jobs() once at startup to register the cron triggers.
"""

import asyncio
import logging
from datetime import datetime

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.pnl import send_daily_report, send_investor_report, send_weekly_report, check_period_reports
from app.rh_keep_alive_state import get_last_run_ts, record_run
from app.rh_pnl import record_rh_equity_snapshot, send_rh_report

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

_KEEP_ALIVE_INTERVAL_SECONDS = 3 * 24 * 60 * 60  # 3 days

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
    """Run rh_client.keep_alive() at most once every 3 days.

    Checked daily via a cron trigger — wall-clock anchored, so restarts
    can't drift the schedule. If a check is missed (app down at the
    scheduled time), the next day's check sees >=3 days elapsed since the
    last run and catches up immediately.
    """
    if not settings.rh_enabled:
        return
    from app.trading.robinhood_client import rh_client
    now_ts = int(datetime.now(ET).timestamp())
    last_run = get_last_run_ts()
    if last_run is not None and now_ts - last_run < _KEEP_ALIVE_INTERVAL_SECONDS:
        return
    await rh_client.keep_alive()
    record_run(now_ts)


async def _quarterly_tax_report() -> None:
    """Post Alpaca + RH tax summaries to their respective channels.

    Jan 1: reports the just-completed year (year - 1).
    Apr 1, Jul 1, Oct 1: reports the current year YTD.
    """
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
        CronTrigger(hour=1, minute=0, timezone=ET),
        id="robinhood_keep_alive",
        replace_existing=True,
    )
    scheduler.add_job(
        _quarterly_tax_report,
        CronTrigger(month="1,4,7,10", day=1, hour=8, minute=0, timezone=ET),
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
