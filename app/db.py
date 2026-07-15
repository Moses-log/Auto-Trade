"""db.py — Single SQLite connection + unit-of-work for the investor ledger.

Source of truth for the four money-critical stores when settings.use_sqlite is
True. A single shared connection (check_same_thread=False) is serialized by a
process-wide RLock; every write happens inside transaction() or writer(). After
a committed write batch, registered exporters regenerate the JSON snapshot files
so the Gist backup and JSON-rollback path stay current.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, List

log = logging.getLogger(__name__)

_DB_PATH = Path(os.getenv("KIMI_DB_PATH", "/data/kimi.db"))
_conn: sqlite3.Connection | None = None
_write_lock = threading.RLock()
_in_txn: contextvars.ContextVar[bool] = contextvars.ContextVar("_in_txn", default=False)
_exporters: List[Callable[[], None]] = []

_SCHEMA = """
CREATE TABLE IF NOT EXISTS investors (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS deposits (
    id          INTEGER PRIMARY KEY,
    investor_id INTEGER NOT NULL REFERENCES investors(id),
    amount      REAL NOT NULL CHECK (amount >= 0),
    entry_spy   REAL NOT NULL,
    date        TEXT NOT NULL,
    seq         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id          INTEGER PRIMARY KEY,
    investor_id INTEGER NOT NULL REFERENCES investors(id),
    units       REAL NOT NULL,
    exit_spy    REAL NOT NULL,
    cost_basis  REAL NOT NULL,
    proceeds    REAL NOT NULL,
    date        TEXT NOT NULL,
    seq         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_withdrawals (
    id           TEXT PRIMARY KEY,
    investor     TEXT NOT NULL,
    amount       REAL NOT NULL CHECK (amount >= 0),
    requested_at TEXT NOT NULL,
    run_at       TEXT NOT NULL,
    spy_price    REAL
);
CREATE TABLE IF NOT EXISTS withdrawal_audit (
    id            INTEGER PRIMARY KEY,
    withdrawal_id TEXT NOT NULL,
    investor      TEXT NOT NULL,
    amount        REAL NOT NULL,
    requested_at  TEXT NOT NULL,
    run_at        TEXT NOT NULL,
    status        TEXT NOT NULL,
    extra_json    TEXT,
    created_at    TEXT NOT NULL
);
"""


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_schema() -> None:
    get_conn().executescript(_SCHEMA)


def register_exporter(fn: Callable[[], None]) -> None:
    if fn not in _exporters:
        _exporters.append(fn)


def _run_exporters() -> None:
    for fn in _exporters:
        try:
            fn()
        except Exception:
            log.exception("db exporter %s failed", getattr(fn, "__name__", fn))


@contextlib.contextmanager
def transaction():
    """All-or-nothing write batch. Nested writer() calls join this transaction."""
    with _write_lock:
        conn = get_conn()
        token = _in_txn.set(True)
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
            _run_exporters()
        finally:
            _in_txn.reset(token)


@contextlib.contextmanager
def writer():
    """Yield the connection for a single write. Autocommits + exports unless
    already inside a transaction(), in which case the outer transaction owns
    the commit and export."""
    if _in_txn.get():
        yield get_conn()
        return
    with _write_lock:
        conn = get_conn()
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
            _run_exporters()


def reset_for_tests() -> None:
    """Close and forget the connection (tests repoint KIMI_DB_PATH)."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
