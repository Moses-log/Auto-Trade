"""
scheduler.py — APScheduler configuration and P&L job registration.

The `scheduler` singleton is started/stopped in main.py's lifespan.
Call setup_jobs() once at startup to register the cron triggers.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, date as _date

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.memprofile import log_snapshot, profile_job
from app.pnl import send_daily_report, send_investor_report, send_weekly_report, check_period_reports
from app.rh_equity_history import get_snapshots
from app.rh_keep_alive_state import get_next_run_ts, record_run
from app.rh_pnl import record_rh_equity_snapshot, send_rh_report
from app.pending_withdrawals import load_pending_withdrawals
from app.trading.alpaca_client import was_market_open_today, get_client, is_first_trading_day_of

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# Imported at module level (not lazily inside the job functions) so tests can
# @patch these names directly on app.scheduler — but wrapped in try/except so
# a startup-time import failure in claude_manager/claude_inspection (e.g. a
# missing dependency) disables only these two cron jobs instead of crashing
# the whole app, since main.py imports this module unconditionally at boot.
try:
    from app.claude_manager import run_monthly_rebalance
except Exception as exc:
    log.error("app.claude_manager failed to import at startup — monthly rebalance disabled: %s", exc)
    run_monthly_rebalance = None

try:
    from app.claude_inspection import run_weekly_inspection
except Exception as exc:
    log.error("app.claude_inspection failed to import at startup — weekly inspection disabled: %s", exc)
    run_weekly_inspection = None

# Reuse claude_manager's canonical log-path constant when it imported successfully
# above (avoids a second, driftable copy of the env var name/default), falling back
# to the same literal if claude_manager failed to import — this constant alone
# carries no meaningful extra startup blast radius since it's a plain os.getenv().
try:
    from app.claude_manager import _LOG_PATH as _REBALANCE_LOG_PATH
except Exception:
    _REBALANCE_LOG_PATH = os.getenv("CLAUDE_REBALANCE_LOG_PATH", "/data/claude_rebalance_log.json")

_INSPECTION_LOG_PATH = os.getenv("CLAUDE_INSPECTION_LOG_PATH", "/data/claude_inspection_log.json")
_COMPLETED_REBALANCE_STATUSES = {"completed", "no_changes", "no_trades"}
_COMPLETED_INSPECTION_STATUSES = {"completed", "no_changes", "no_holdings"}


def _already_decided_this_period(log_path: str, period_start, completed_statuses: set) -> bool:
    """True if the log's most recent entry already finalized a decision for
    a period starting on/after period_start.

    Belt-and-suspenders guard: is_first_trading_day_of() fails open (returns
    True) on a transient Alpaca API error, which could let a later day in a
    cron window re-fire a full duplicate real-money run after an earlier day
    already completed. Pre-decision failures (fetch/API/parse errors, RH
    session down) are excluded from completed_statuses so the window can
    still retry them.
    """
    try:
        with open(log_path) as f:
            records = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s for idempotency check — treating as not yet run: %s", log_path, exc)
        return False
    if not records:
        return False
    last = records[-1]
    if last.get("status") not in completed_statuses:
        return False
    try:
        entry_date = datetime.fromisoformat(last.get("timestamp", "")).date()
    except ValueError as exc:
        log.warning("Could not parse timestamp in %s for idempotency check: %s", log_path, exc)
        return False
    return entry_date >= period_start


def _rebalance_already_completed_this_month() -> bool:
    return _already_decided_this_period(
        _REBALANCE_LOG_PATH, _date.today().replace(day=1), _COMPLETED_REBALANCE_STATUSES,
    )


def _inspection_already_completed_this_week() -> bool:
    today = _date.today()
    week_start = today - timedelta(days=today.weekday())
    return _already_decided_this_period(_INSPECTION_LOG_PATH, week_start, _COMPLETED_INSPECTION_STATUSES)


# Singleton — imported and started in main.py lifespan.
scheduler = AsyncIOScheduler(timezone=ET)


def _profiled(tag: str, fn):
    """Wrap a scheduler coroutine so its RSS before/after (and tracemalloc
    top allocators) get logged. Temporary — part of the memory-leak probe in
    app/memprofile.py. Remove this and the log_snapshot job once the leaking
    site is identified and fixed."""

    async def _runner():
        async with profile_job(tag):
            return await fn()

    _runner.__name__ = getattr(fn, "__name__", tag)
    return _runner


async def _memprofile_hourly() -> None:
    """Hourly memory snapshot — catches steady baseline creep between jobs and
    shows which allocation site climbs over days. Temporary probe."""
    log_snapshot("hourly")


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

    Fires on the first trading day of Jan/Apr/Jul/Oct (cron covers days 1-4
    to handle cases where the 1st is a holiday or weekend — widened from 1-3
    to also cover a Friday New Year's Day, where Fri holiday + Sat + Sun
    pushes the first trading day to the 4th, e.g. Jan 2027).

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
    """Fires on the first trading day of the month (cron covers days 1-4 to
    handle cases where the 1st is a holiday or weekend — same pattern as
    _quarterly_tax_report, widened one extra day to cover a Friday New
    Year's Day, where Fri holiday + Sat + Sun pushes the first trading day
    to the 4th, e.g. Jan 2027)."""
    if not was_market_open_today():
        log.info("_claude_monthly_rebalance: market holiday — skipping")
        return
    if not is_first_trading_day_of(_date.today().replace(day=1)):
        log.info("_claude_monthly_rebalance: not first trading day of month — skipping")
        return
    if _rebalance_already_completed_this_month():
        log.warning("_claude_monthly_rebalance: log shows this month already completed — skipping duplicate run")
        return
    if run_monthly_rebalance is None:
        log.error("_claude_monthly_rebalance: claude_manager failed to import at startup — skipping")
        return
    await run_monthly_rebalance()


async def _weekly_inspection() -> None:
    """Fires on the first trading day of the week (cron covers Mon-Wed to
    handle a Monday/Tuesday holiday). Skipped when today is also the first
    trading day of the month — the monthly rebalance owns that week."""
    if not was_market_open_today():
        log.info("_weekly_inspection: market holiday — skipping")
        return
    today = _date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday of this week
    if not is_first_trading_day_of(week_start):
        log.info("_weekly_inspection: not first trading day of week — skipping")
        return
    if is_first_trading_day_of(today.replace(day=1)):
        log.info("_weekly_inspection: coincides with monthly rebalance day — skipping")
        return
    if _inspection_already_completed_this_week():
        log.warning("_weekly_inspection: log shows this week already completed — skipping duplicate run")
        return
    if run_weekly_inspection is None:
        log.error("_weekly_inspection: claude_inspection failed to import at startup — skipping")
        return
    await run_weekly_inspection()


async def _nightly_backup() -> None:
    from app.backup import push_backup
    await push_backup()


def setup_jobs() -> None:
    """Register cron jobs — two parallel bundles replacing five staggered jobs."""
    scheduler.add_job(
        _profiled("weekday_jobs", _weekday_jobs),
        CronTrigger(day_of_week="mon-thu", hour=16, minute=0, timezone=ET),
        id="weekday_jobs",
        replace_existing=True,
    )
    scheduler.add_job(
        _profiled("friday_jobs", _friday_jobs),
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
        CronTrigger(month="1,4,7,10", day="1-4", hour=8, minute=0, timezone=ET),
        id="quarterly_tax_report",
        replace_existing=True,
    )
    scheduler.add_job(
        _profiled("claude_monthly_rebalance", _claude_monthly_rebalance),
        CronTrigger(day="1-4", hour=9, minute=35, timezone=ET),
        id="claude_monthly_rebalance",
        replace_existing=True,
    )
    scheduler.add_job(
        _profiled("weekly_inspection", _weekly_inspection),
        CronTrigger(day_of_week="mon-wed", hour=9, minute=35, timezone=ET),
        id="weekly_inspection",
        replace_existing=True,
    )
    scheduler.add_job(
        _profiled("nightly_backup", _nightly_backup),
        CronTrigger(hour=0, minute=0, timezone=ET),
        id="nightly_backup",
        replace_existing=True,
    )
    scheduler.add_job(
        _memprofile_hourly,
        CronTrigger(minute=0, timezone=ET),
        id="memprofile_hourly",
        replace_existing=True,
    )
    log.info(
        "Scheduler jobs registered: weekday_jobs, friday_jobs (Alpaca+RH), "
        "robinhood_keep_alive (daily check, runs every ~3 days), "
        "quarterly_tax_report (first trading day of Jan/Apr/Jul/Oct), "
        "claude_monthly_rebalance (first trading day of each month, 9:35 AM ET), "
        "weekly_inspection (first trading day of week, 9:35 AM ET, skipped on rebalance weeks), "
        "nightly_backup (daily midnight ET), "
        "memprofile_hourly (temporary memory-leak probe, top of every hour)"
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
