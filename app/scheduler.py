"""
scheduler.py — APScheduler configuration and P&L job registration.

The `scheduler` singleton is started/stopped in main.py's lifespan.
Call setup_jobs() once at startup to register the cron triggers.
"""

import logging

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.pnl import send_daily_report, send_investor_report, send_weekly_report

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
    log.info("P&L scheduler jobs registered: daily_pnl (Mon-Fri 16:00 ET), weekly_pnl (Fri 16:01 ET)")
