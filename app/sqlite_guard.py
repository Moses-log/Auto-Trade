"""sqlite_guard.py — Startup safety check for the SQLite ledger backend.

When USE_SQLITE is on, `ensure_sqlite_ready()` (called once from main.py's
lifespan) makes activating SQLite foolproof:

  * It runs `db.init_schema()` (idempotent) so the tables always exist — a
    legitimately-migrated or genuinely-fresh system never throws "no such table".
  * It guards against the "flag flipped before migration" state: if the SQLite
    ledger is empty while the JSON ledger still holds investors, it falls back to
    JSON for THIS process (sets settings.use_sqlite = False so every read/write
    uses the intact JSON path — no clobber) and fires a CRITICAL alert telling the
    operator to run scripts/migrate_to_sqlite and restart.

See docs/runbooks/sqlite-cutover.md. Investor count is the readiness signal: the
migration writes all four stores in one transaction, so investors present ⇒ the
migration ran.
"""
from __future__ import annotations

import logging

from app.config import settings

log = logging.getLogger(__name__)

_NOT_READY_ALERT = (
    "🚨 **SQLITE LEDGER NOT READY**\n"
    "`USE_SQLITE=true` but the SQLite ledger is empty while `investors.json` still "
    "has data — this instance is running on JSON to avoid data loss.\n"
    "Run `python -m scripts.migrate_to_sqlite` in the Render shell (confirm the "
    "`VERIFIED` line), then restart to activate SQLite."
)


async def ensure_sqlite_ready(notifier=None) -> bool:
    """Prepare/validate the SQLite backend at startup.

    Returns True if this process will use SQLite, False if it will use JSON
    (either the flag was off, or the empty-DB guard tripped and fell back).

    `notifier` is an async callable taking a message string; defaults to
    notify_rh_session (Private Server). Injectable for tests.
    """
    if not settings.use_sqlite:
        return False

    # Any failure in the readiness check must NOT take down startup: on the
    # "not migrated" state (or any unexpected error) we fall back to JSON and
    # alert, never crash.
    try:
        from app import db
        from app.investors import _load_investors_json

        db.init_schema()

        sqlite_investor_count = db.get_conn().execute(
            "SELECT COUNT(*) FROM investors"
        ).fetchone()[0]
        if sqlite_investor_count > 0:
            log.info("SQLite ledger ready (%d investors).", sqlite_investor_count)
            return True

        # SQLite is empty — legitimate only if the JSON ledger is also empty (a
        # genuinely fresh system). If JSON has data, the migration was not run.
        json_investor_count = len(_load_investors_json())
        if json_investor_count == 0:
            log.info("SQLite ledger empty and JSON ledger empty — fresh system, using SQLite.")
            return True

        reason = f"the SQLite ledger is empty while JSON has {json_investor_count} investors"
    except Exception:
        log.exception("sqlite_guard: SQLite readiness check failed")
        reason = "the SQLite readiness check raised an error"

    # Fall back to JSON for this process and alert loudly.
    log.critical(
        "USE_SQLITE=true but %s — falling back to JSON. Run scripts/migrate_to_sqlite "
        "and restart to activate SQLite.",
        reason,
    )
    settings.use_sqlite = False

    if notifier is None:
        from app.notifications import notify_rh_session as notifier
    try:
        await notifier(_NOT_READY_ALERT)
    except Exception:
        log.exception("sqlite_guard: failed to send 'ledger not ready' alert")

    return False
