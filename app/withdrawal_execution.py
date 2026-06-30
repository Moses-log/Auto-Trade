from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

from apscheduler.jobstores.base import JobLookupError

from app.backup import push_backup
from app.config import settings
from app.investors import (
    Withdrawal,
    compute_nav_per_unit,
    compute_withdrawal_lots,
    format_withdrawal_message,
    load_investors,
    save_investors,
    investors_lock,
)
from app.notifications import notify_investors
from app.pending_withdrawals import (
    get_pending_withdrawal,
    remove_pending_withdrawal,
    save_pending_withdrawal,
)
from app.scheduler import scheduler
from app.trading.alpaca_client import get_account, get_latest_price
from app.withdrawal_audit import append_withdrawal_audit

log = logging.getLogger(__name__)

_CT = pytz.timezone("America/Chicago")


class WithdrawalValidationError(Exception):
    """Raised by schedule_withdrawal when the request can't be scheduled."""


class WithdrawalNotFoundError(Exception):
    """Raised by cancel_pending_withdrawal when the id doesn't match a pending withdrawal."""


async def schedule_withdrawal(
    investor_name: str,
    amount: float,
    spy_price: Optional[float] = None,
) -> dict:
    if amount <= 0:
        raise WithdrawalValidationError("Withdrawal amount must be positive")

    # Track whether the caller explicitly provided a price (lock-in) vs we fetch for validation only
    locked_spy_price = spy_price

    validation_spy = spy_price
    if validation_spy is None:
        validation_spy = get_latest_price("SPY")
        if validation_spy is None:
            raise WithdrawalValidationError("Could not fetch SPY price — try again")

    investors = load_investors()
    inv = next((i for i in investors if i.name.lower() == investor_name.lower()), None)
    if inv is None:
        raise WithdrawalValidationError(f'Investor "{investor_name}" not found — check spelling')

    try:
        account = get_account()
        real_total_equity = float(account.equity)
    except Exception as exc:
        raise WithdrawalValidationError("Could not fetch account equity — try again") from exc
    nav_per_unit = compute_nav_per_unit(investors, real_total_equity)

    try:
        # Validation only — the result is discarded. Execution re-runs this with
        # a live price/equity reading and the investor's state at execution time, not now.
        compute_withdrawal_lots(inv, amount, nav_per_unit)
    except ValueError as exc:
        raise WithdrawalValidationError(str(exc)) from exc

    now = datetime.now(_CT)
    run_at = now + timedelta(hours=settings.withdrawal_delay_hours)
    withdrawal_id = f"wd-{uuid.uuid4().hex[:8]}"

    save_pending_withdrawal(
        withdrawal_id=withdrawal_id,
        investor=inv.name,
        amount=amount,
        requested_at=now.isoformat(),
        run_at=run_at.isoformat(),
        spy_price=locked_spy_price,  # only stored if user explicitly provided it
    )

    scheduler.add_job(
        execute_pending_withdrawal,
        "date",
        run_date=run_at,
        args=[withdrawal_id],
        id=f"withdrawal_{withdrawal_id}",
        replace_existing=True,
    )

    log.info("Scheduled withdrawal %s for %s ($%.2f) at %s", withdrawal_id, inv.name, amount, run_at)
    return {
        "id": withdrawal_id,
        "investor": inv.name,
        "amount": amount,
        "requested_at": now.isoformat(),
        "run_at": run_at.isoformat(),
    }


async def execute_pending_withdrawal(withdrawal_id: str) -> None:
    record = get_pending_withdrawal(withdrawal_id)
    if record is None:
        log.warning("execute_pending_withdrawal: %s not found (already canceled?)", withdrawal_id)
        return

    # Use the fill price locked in at schedule time if provided; otherwise fetch live
    spy_price: Optional[float] = record.get("spy_price") or get_latest_price("SPY")

    real_total_equity = None
    try:
        account = get_account()
        real_total_equity = float(account.equity)
    except Exception as exc:
        log.warning(
            "execute_pending_withdrawal: could not fetch account equity for %s: %s",
            withdrawal_id, exc,
        )

    if spy_price is None or real_total_equity is None:
        log.error(
            "execute_pending_withdrawal: market/account data unavailable for %s — retrying in 15 minutes",
            withdrawal_id,
        )
        retry_at = datetime.now(_CT) + timedelta(minutes=15)
        try:
            scheduler.add_job(
                execute_pending_withdrawal,
                "date",
                run_date=retry_at,
                args=[withdrawal_id],
                id=f"withdrawal_{withdrawal_id}",
                replace_existing=True,
            )
        except Exception:
            log.exception("execute_pending_withdrawal: failed to schedule retry for %s", withdrawal_id)
        try:
            await notify_investors(
                f"⚠️ Scheduled withdrawal for {record['investor']} (${record['amount']:,.2f}) "
                f"could not execute — market data unavailable. Retrying at "
                f"{retry_at.strftime('%b %d, %I:%M %p %Z')}."
            )
        except Exception:
            log.exception("execute_pending_withdrawal: failed to send price-unavailable notification for %s", withdrawal_id)
        return

    discord_msg = None
    error_reason = None

    async with investors_lock:
        investors = load_investors()
        inv = next((i for i in investors if i.name.lower() == record["investor"].lower()), None)
        if inv is None:
            error_reason = f'Investor "{record["investor"]}" no longer exists'
        else:
            nav_per_unit = compute_nav_per_unit(investors, real_total_equity)
            try:
                lots, units_redeemed = compute_withdrawal_lots(inv, record["amount"], nav_per_unit)
            except ValueError as exc:
                error_reason = str(exc)
            else:
                try:
                    total_cost_basis = sum(lot["cost"] for lot in lots)
                    discord_msg = format_withdrawal_message(
                        inv, lots, units_redeemed, spy_price, nav_per_unit, record["amount"]
                    )
                    inv.withdrawals.append(
                        Withdrawal(
                            units=units_redeemed,
                            exit_spy=spy_price,
                            cost_basis=total_cost_basis,
                            proceeds=record["amount"],
                            date=date.today().isoformat(),
                        )
                    )
                    save_investors(investors)
                except Exception as exc:
                    log.exception(
                        "execute_pending_withdrawal: save_investors failed for %s — no funds moved, marking failed",
                        withdrawal_id,
                    )
                    error_reason = f"Internal error while recording withdrawal: {exc}"
                    discord_msg = None

    try:
        remove_pending_withdrawal(withdrawal_id)
    except Exception:
        log.exception("execute_pending_withdrawal: failed to remove pending record %s after processing", withdrawal_id)

    if error_reason:
        try:
            append_withdrawal_audit(
                withdrawal_id=record["id"], investor=record["investor"], amount=record["amount"],
                requested_at=record["requested_at"], run_at=record["run_at"],
                status="failed", reason=error_reason,
            )
        except Exception:
            log.exception("execute_pending_withdrawal: failed to write 'failed' audit entry for %s", withdrawal_id)
        try:
            await notify_investors(
                f"❌ Scheduled withdrawal for {record['investor']} (${record['amount']:,.2f}) failed: {error_reason}"
            )
        except Exception:
            log.exception("execute_pending_withdrawal: failed to send failure notification for %s", withdrawal_id)
        return

    try:
        append_withdrawal_audit(
            withdrawal_id=record["id"], investor=record["investor"], amount=record["amount"],
            requested_at=record["requested_at"], run_at=record["run_at"],
            status="executed", completed_at=datetime.now(_CT).isoformat(),
        )
    except Exception:
        log.exception(
            "execute_pending_withdrawal: withdrawal %s WAS EXECUTED (funds moved) but audit write failed — manual reconciliation needed",
            withdrawal_id,
        )

    try:
        await notify_investors(discord_msg)
    except Exception:
        log.exception("execute_pending_withdrawal: notify_investors failed for executed withdrawal %s", withdrawal_id)

    try:
        await push_backup()
    except Exception:
        log.exception("execute_pending_withdrawal: push_backup failed after withdrawal %s", withdrawal_id)


async def cancel_pending_withdrawal(withdrawal_id: str) -> dict:
    record = get_pending_withdrawal(withdrawal_id)
    if record is None:
        raise WithdrawalNotFoundError(
            f"No pending withdrawal with id {withdrawal_id} (already executed, canceled, or never existed)"
        )

    try:
        scheduler.remove_job(f"withdrawal_{withdrawal_id}")
    except JobLookupError:
        pass  # job already fired or was already removed; still clean up the record below

    remove_pending_withdrawal(withdrawal_id)
    append_withdrawal_audit(
        withdrawal_id=record["id"], investor=record["investor"], amount=record["amount"],
        requested_at=record["requested_at"], run_at=record["run_at"],
        status="canceled", canceled_at=datetime.now(_CT).isoformat(),
    )
    log.info("Canceled pending withdrawal %s for %s", withdrawal_id, record["investor"])
    return record
