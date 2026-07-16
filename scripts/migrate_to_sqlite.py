"""migrate_to_sqlite.py — One-way, offline migration of the investor ledger
JSON stores into /data/kimi.db, with exhaustive verification.

Run manually (never on deploy):
    USE_SQLITE=false python -m scripts.migrate_to_sqlite

The script reads JSON via the existing loaders with the flag OFF, writes to
SQLite, then verifies every field and every computed financial figure matches
before declaring success. Exits non-zero on any mismatch. JSON is never mutated;
a pre-migration copy is saved to /data/backup_pre_sqlite/.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from app import db
from app.config import settings

# Fixed synthetic equity so both JSON and SQLite compute compute_breakdown identically.
_VERIFY_EQUITY = 1_000_000.0
_CENTS = 2

_BACKUP_DIR = Path(os.getenv("PRE_SQLITE_BACKUP_DIR", "/data/backup_pre_sqlite"))
_SOURCE_FILES = ["INVESTORS_PATH", "PENDING_WITHDRAWALS_PATH",
                 "WITHDRAWAL_AUDIT_PATH", "RH_DEPOSIT_LOG_PATH"]
_DEFAULT_PATHS = {
    "INVESTORS_PATH": "/data/investors.json",
    "PENDING_WITHDRAWALS_PATH": "/data/pending_withdrawals.json",
    "WITHDRAWAL_AUDIT_PATH": "/data/withdrawal_audit.json",
    "RH_DEPOSIT_LOG_PATH": "/data/rh_deposits.json",
}


def _read_json_stores():
    """Load all four stores from JSON (flag forced OFF)."""
    prev = settings.use_sqlite
    settings.use_sqlite = False
    try:
        from app.investors import load_investors
        from app.pending_withdrawals import load_pending_withdrawals
        from app.withdrawal_audit import load_withdrawal_audit
        from app.rh_deposit_log import load_rh_deposits
        return (load_investors(), load_pending_withdrawals(),
                load_withdrawal_audit(), load_rh_deposits())
    finally:
        settings.use_sqlite = prev


def _write_sqlite_stores(investors, pending, audit, rh_deposits):
    prev = settings.use_sqlite
    settings.use_sqlite = True
    try:
        from app.investors import save_investors
        from app.pending_withdrawals import _save_sqlite as save_pending
        from app.withdrawal_audit import _append_sqlite as append_audit
        from app.rh_deposit_log import _write_sqlite as write_rh
        with db.transaction():
            save_investors(investors)
            save_pending(pending)
            for entry in audit:
                append_audit(entry)
            write_rh(rh_deposits)
    finally:
        settings.use_sqlite = prev


def _backup_json():
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for key in _SOURCE_FILES:
        src = Path(os.getenv(key, _DEFAULT_PATHS[key]))
        if src.exists():
            shutil.copy2(src, _BACKUP_DIR / src.name)


def migrate() -> dict:
    db.init_schema()
    _backup_json()
    investors, pending, audit, rh_deposits = _read_json_stores()
    _write_sqlite_stores(investors, pending, audit, rh_deposits)
    return {
        "investors": len(investors),
        "deposits": sum(len(i.deposits) for i in investors),
        "withdrawals": sum(len(i.withdrawals) for i in investors),
        "pending": len(pending),
        "audit": len(audit),
        "rh_deposits": len(rh_deposits),
    }


def _breakdown_dict(investors):
    from app.investors import compute_breakdown
    b = compute_breakdown(investors, spy_price=500.0, real_total_equity=_VERIFY_EQUITY)
    return {
        r.name: (round(r.total_deposited, _CENTS), round(r.current_equity, _CENTS),
                 round(r.dollar_pnl, _CENTS), round(r.total_withdrawn, _CENTS),
                 round(r.portfolio_share, _CENTS))
        for r in b.investors
    }


def verify_migration() -> list[str]:
    """Return a list of mismatch descriptions (empty list = verified)."""
    mismatches: list[str] = []

    json_inv, json_pending, json_audit, json_rh = _read_json_stores()

    prev = settings.use_sqlite
    settings.use_sqlite = True
    try:
        from app.investors import load_investors, serialize_investors
        from app.pending_withdrawals import load_pending_withdrawals
        from app.withdrawal_audit import load_withdrawal_audit
        from app.rh_deposit_log import load_rh_deposits
        db_inv = load_investors()
        db_pending = load_pending_withdrawals()
        db_audit = load_withdrawal_audit()
        db_rh = load_rh_deposits()

        # 1. Structural + field-level: serialize_investors is a total, ordered dump.
        if serialize_investors(json_inv) != serialize_investors(db_inv):
            mismatches.append("investors: field-level mismatch (deposits/withdrawals differ)")

        # 2. Pending / audit / rh deposits (list equality after normalising order)
        if sorted(json_pending, key=lambda r: r["id"]) != sorted(db_pending, key=lambda r: r["id"]):
            mismatches.append("pending_withdrawals: mismatch")
        if sorted(json_audit, key=lambda r: (r["id"], r["status"])) != \
           sorted(db_audit, key=lambda r: (r["id"], r["status"])):
            mismatches.append("withdrawal_audit: mismatch")
        if sorted(json_rh, key=lambda d: (d["date"], d["amount"])) != \
           sorted(db_rh, key=lambda d: (d["date"], d["amount"])):
            mismatches.append("rh_deposits: mismatch")

        # 3. Computed financial figures to the cent.
        if _breakdown_dict(json_inv) != _breakdown_dict(db_inv):
            mismatches.append("compute_breakdown: financial figures differ")
    finally:
        settings.use_sqlite = prev

    return mismatches


def main() -> int:
    counts = migrate()
    print("Migrated:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    mismatches = verify_migration()
    if mismatches:
        print("VERIFICATION FAILED:")
        for m in mismatches:
            print("   -", m)
        return 1
    print(f"VERIFIED — investors={counts['investors']} "
          f"deposits={counts['deposits']} withdrawals={counts['withdrawals']} "
          f"(pending={counts['pending']} audit={counts['audit']} rh_deposits={counts['rh_deposits']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
