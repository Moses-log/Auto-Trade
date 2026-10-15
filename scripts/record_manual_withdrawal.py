"""record_manual_withdrawal.py — Record a withdrawal in the investor ledger
after the cash already left Alpaca (e.g. a scheduled withdrawal failed).

Dry run by default; prints what it would write. Add --apply to write.
Run on Render (Shell tab), from the repo root:

    python -m scripts.record_manual_withdrawal --investor David \\
        --amount 4163.02 --exit-spy 777.66 --date 2026-10-13 --all-units
    python -m scripts.record_manual_withdrawal ... --apply

--all-units redeems every unit the investor holds (a full exit). --units N
redeems exactly N. Proceeds are exactly --amount; cost basis is FIFO across
the investor's deposit lots. On --apply the ledger write, removal of the
investor's stale pending withdrawals, and an 'executed_manual' audit entry
commit together (one SQLite transaction), then the Gist backup is pushed.
Refuses to run if the investor holds no units, so it can't double-record.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from datetime import date, datetime

import pytz

from app import db
from app.config import settings
from app.investors import (
    Withdrawal, _net_units, compute_withdrawal_lots, load_investors, save_investors,
)
from app.pending_withdrawals import load_pending_withdrawals, remove_pending_withdrawal
from app.withdrawal_audit import append_withdrawal_audit

_CT = pytz.timezone("America/Chicago")


def _parse(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--investor", required=True)
    p.add_argument("--amount", required=True, type=float, help="cash paid out, dollars")
    p.add_argument("--exit-spy", required=True, type=float, help="SPY price the cash was locked at")
    p.add_argument("--date", required=True, help="ISO date of the withdrawal, e.g. 2026-10-13")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--all-units", action="store_true", help="redeem every unit held")
    g.add_argument("--units", type=float, help="redeem exactly this many units")
    p.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    p.add_argument("--no-backup", action="store_true", help="skip the Gist backup push")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(argv)
    if args.amount <= 0 or args.exit_spy <= 0:
        print("❌ --amount and --exit-spy must be positive")
        return 1
    date.fromisoformat(args.date)  # raises on a bad date

    if settings.use_sqlite:
        db.init_schema()

    investors = load_investors()
    inv = next((i for i in investors if i.name.lower() == args.investor.lower()), None)
    if inv is None:
        print(f'❌ Investor "{args.investor}" not found')
        return 1

    held = _net_units(inv)
    if held <= 1e-9:
        print(f"❌ {inv.name} holds no units — nothing to redeem (already recorded?)")
        return 1
    units = held if args.all_units else args.units

    try:
        lots, units_redeemed = compute_withdrawal_lots(inv, args.amount, 0.0, units=units)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1

    cost_basis = sum(l["cost"] for l in lots)
    stale = [r for r in load_pending_withdrawals() if r["investor"].lower() == inv.name.lower()]

    print(f"{'APPLY' if args.apply else 'DRY RUN'} — {inv.name}")
    print(f"  units held        {held:.6f}")
    print(f"  units redeemed    {units_redeemed:.6f}  (left: {held - units_redeemed:.6f})")
    print(f"  proceeds          ${args.amount:,.2f}  @ SPY ${args.exit_spy:,.2f}  on {args.date}")
    print(f"  FIFO cost basis   ${cost_basis:,.2f}  ·  realized ${args.amount - cost_basis:+,.2f}")
    for r in stale:
        print(f"  remove pending    {r['id']}  (${r['amount']:,.2f}, run_at {r['run_at']})")

    if not args.apply:
        print("Nothing written. Re-run with --apply to record it.")
        return 0

    inv.withdrawals.append(Withdrawal(
        units=units_redeemed, exit_spy=args.exit_spy, cost_basis=cost_basis,
        proceeds=args.amount, date=args.date,
    ))
    now = datetime.now(_CT).isoformat()
    txn = db.transaction() if settings.use_sqlite else contextlib.nullcontext()
    with txn:
        save_investors(investors)
        for r in stale:
            remove_pending_withdrawal(r["id"])
        append_withdrawal_audit(
            withdrawal_id=stale[0]["id"] if stale else f"manual-{args.date}",
            investor=inv.name, amount=args.amount,
            requested_at=stale[0]["requested_at"] if stale else now,
            run_at=stale[0]["run_at"] if stale else now,
            status="executed_manual", completed_at=now,
            units=units_redeemed, exit_spy=args.exit_spy,
        )
    print(f"✅ Recorded. {inv.name} now holds {_net_units(inv):.6f} units.")

    if not args.no_backup:
        from app.backup import push_backup
        try:
            print(f"  backup: {asyncio.run(push_backup())}")
        except Exception as exc:
            print(f"⚠️ Ledger written but Gist backup failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
