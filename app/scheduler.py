"""
scheduler.py — APScheduler configuration and P&L job registration.

The `scheduler` singleton is started/stopped in main.py's lifespan.
Call setup_jobs() once at startup to register the cron triggers.
"""

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


def setup_jobs() -> None:
    """Register daily and weekly P&L cron jobs.

    Daily:  Mon–Fri at 16:00 ET
    Weekly: Friday  at 16:01 ET (1 minute after daily to ensure order)
    """
    scheduler.add_job(
        send_daily_report,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=ET),
        id="daily_pnl",
        replace_existing=True,
    )
    scheduler.add_job(
        send_weekly_report,
        CronTrigger(day_of_week="fri", hour=16, minute=1, timezone=ET),
        id="weekly_pnl",
        replace_existing=True,
    )
    scheduler.add_job(
        send_investor_report,
        CronTrigger(day_of_week="mon-thu", hour=16, minute=2, timezone=ET),
        id="investor_breakdown_daily",
        replace_existing=True,
    )
    scheduler.add_job(
        send_investor_report,
        CronTrigger(day_of_week="fri", hour=16, minute=3, timezone=ET),
        id="investor_breakdown_weekly",
        replace_existing=True,
    )
    scheduler.add_job(
        check_period_reports,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=5, timezone=ET),
        id="period_pnl_check",
        replace_existing=True,
    )
    log.info("P&L scheduler jobs registered: daily_pnl, weekly_pnl, period_pnl_check (Mon-Fri 16:05 ET)")


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
