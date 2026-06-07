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

from app.pnl import send_daily_report, send_investor_report, send_weekly_report, check_period_reports

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# Singleton — imported and started in main.py lifespan.
scheduler = AsyncIOScheduler(timezone=ET)


async def _weekday_jobs() -> None:
    """Run Mon–Thu reports in parallel: daily P&L, investor breakdown, period check."""
    await asyncio.gather(
        send_daily_report(),
        send_investor_report(),
        check_period_reports(),
        return_exceptions=True,
    )


async def _friday_jobs() -> None:
    """Run Friday reports in parallel: daily, weekly, investor breakdown, period check."""
    await asyncio.gather(
        send_daily_report(),
        send_weekly_report(),
        send_investor_report(),
        check_period_reports(),
        return_exceptions=True,
    )


async def _robinhood_keep_alive() -> None:
    from app.trading.robinhood_client import rh_client
    await rh_client.keep_alive()


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
        CronTrigger(day="*/3", hour=3, minute=0, timezone=ET),
        id="robinhood_keep_alive",
        replace_existing=True,
    )
    log.info("Scheduler jobs registered: weekday_jobs, friday_jobs, robinhood_keep_alive (every 3 days 03:00 ET)")


def reschedule_pending_orders() -> None:
    from app.pending_orders import load_pending_orders
    from app.trade_notifier import notify_pending_order_fill

    orders = load_pending_orders()
    if not orders:
        return

    now = datetime.now(ET)
    for entry in orders:
        order_id = entry["order_id"]
        try:
            run_dt = datetime.fromisoformat(entry["run_at"])
            if run_dt.tzinfo is None:
                run_dt = ET.localize(run_dt)
        except Exception:
            run_dt = now

        effective_run = run_dt if run_dt > now else now

        scheduler.add_job(
            notify_pending_order_fill,
            "date",
            run_date=effective_run,
            args=[order_id, entry["ticker"], entry["action"],
                  entry.get("alert_price"), entry.get("avg_entry_price")],
            id=f"pending_{order_id}",
            replace_existing=True,
        )
        log.info("Rescheduled pending order %s for %s", order_id, effective_run)
