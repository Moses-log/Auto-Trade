# Investor Ledger → SQLite Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the four money-critical JSON stores (investors, pending withdrawals, withdrawal audit, RH deposits) into a single SQLite database so the withdrawal write path is atomic, with zero behavior change and instant rollback.

**Architecture:** A new `app/db.py` owns one SQLite connection (WAL, foreign keys on), a `transaction()` context manager, an autocommitting `writer()`, and an exporter registry that regenerates the JSON files after every committed write. Each of the four store modules gains a SQLite-backed load/save path selected by a `USE_SQLITE` flag (default off); the JSON path is untouched. `withdrawal_execution.py` wraps its three writes in one `transaction()`. SQLite is the source of truth; the JSON files become derived read-only exports used by the existing Gist backup and by rollback.

**Tech Stack:** Python 3.9+, stdlib `sqlite3`, `contextvars`, FastAPI, APScheduler, pytest / pytest-asyncio.

## Global Constraints

- **No domain math changes.** Do not modify `compute_nav_per_unit`, `compute_withdrawal_lots`, `compute_time_weighted_capital`, `compute_breakdown`, FIFO tax-lot logic, or any Discord/report formatting.
- **Signatures preserved.** `load_investors()`, `save_investors()`, `load_pending_withdrawals()`, `save_pending_withdrawal()`, `remove_pending_withdrawal()`, `get_pending_withdrawal()`, `append_withdrawal_audit()`, `load_withdrawal_audit()`, `load_rh_deposits()`, `append_rh_deposit()`, `clear_rh_deposits()`, `get_rh_deposit_events()` keep their existing signatures.
- **`USE_SQLITE` defaults to `false`.** With the flag off, behavior is byte-for-byte the current JSON behavior.
- **Money compared to the cent.** All financial equality checks round to 2 decimals; everything else exact.
- **Deposit/withdrawal ordering is significant** (FIFO). Preserve list order via `seq` columns.
- **DB path:** `/data/kimi.db`, overridable via `KIMI_DB_PATH` env var (tests set this to a tmp path).
- **Commit after every task.** TDD: failing test first, minimal code, passing test, commit.
- Work on branch `feature/investor-ledger-sqlite` (already created).

---

## File Structure

- **Create** `app/db.py` — connection, pragmas, schema, `transaction()`, `writer()`, exporter registry.
- **Create** `scripts/migrate_to_sqlite.py` — one-way offline migrate + `verify_migration()`.
- **Create** tests: `tests/test_db.py`, `tests/test_investors_sqlite.py`, `tests/test_pending_withdrawals_sqlite.py`, `tests/test_withdrawal_audit_sqlite.py`, `tests/test_rh_deposit_log_sqlite.py`, `tests/test_withdrawal_atomicity.py`, `tests/test_migrate_to_sqlite.py`, `tests/test_backup_db.py`, `tests/test_backend_parity.py`.
- **Modify** `app/config.py` — add `use_sqlite` flag.
- **Modify** `app/investors.py` — SQLite load/save + JSON exporter, flag-selected.
- **Modify** `app/pending_withdrawals.py` — same treatment.
- **Modify** `app/withdrawal_audit.py` — same treatment (incl. `extra_json`).
- **Modify** `app/rh_deposit_log.py` — same treatment.
- **Modify** `app/withdrawal_execution.py` — wrap the three writes in `db.transaction()`.
- **Modify** `app/backup.py` — add base64 `kimi.db` to the Gist payload.

---

## Task 1: `app/db.py` — connection, schema, transaction, exporter registry

**Files:**
- Create: `app/db.py`
- Modify: `app/config.py` (add `use_sqlite`)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `app.config.settings`.
- Produces:
  - `get_conn() -> sqlite3.Connection` — the shared connection (reads see uncommitted writes in an open transaction).
  - `transaction()` — context manager; `BEGIN` on enter, `COMMIT` + run exporters on clean exit, `ROLLBACK` on exception. Nested writes via `writer()` join it.
  - `writer()` — context manager yielding the connection; if inside a `transaction()` it just yields (no commit); otherwise it commits and runs exporters on clean exit.
  - `register_exporter(fn: Callable[[], None]) -> None` — register a JSON-export callback run after every committed write batch.
  - `init_schema() -> None` — create tables idempotently.
  - `reset_for_tests() -> None` — close/forget the connection (tests point `KIMI_DB_PATH` at a fresh tmp file).

- [ ] **Step 1: Add the config flag**

In `app/config.py`, inside `class Settings`, after the `allow_fractional_shares` block, add:

```python
    # ── Storage backend ───────────────────────────────────────────────────────
    # When True, the investor ledger (investors / pending withdrawals / audit /
    # rh deposits) is stored in SQLite (/data/kimi.db) and the JSON files become
    # derived read-only exports. Default False = pure-JSON behavior (unchanged).
    use_sqlite: bool = False
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_db.py`:

```python
import importlib
import os
import sqlite3

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    yield db
    db.reset_for_tests()


def test_schema_has_all_tables(db):
    conn = db.get_conn()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"investors", "deposits", "withdrawals",
            "pending_withdrawals", "withdrawal_audit"} <= names


def test_foreign_keys_and_wal_enabled(db):
    conn = db.get_conn()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_transaction_commits(db):
    with db.transaction():
        db.get_conn().execute("INSERT INTO investors(name) VALUES('Alice')")
    rows = db.get_conn().execute("SELECT name FROM investors").fetchall()
    assert [r[0] for r in rows] == ["Alice"]


def test_transaction_rolls_back_and_leaves_db_untouched(db):
    db.get_conn().execute("INSERT INTO investors(name) VALUES('Seed')")
    db.get_conn().commit()
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.get_conn().execute("INSERT INTO investors(name) VALUES('Bob')")
            raise RuntimeError("boom")
    rows = db.get_conn().execute("SELECT name FROM investors").fetchall()
    assert [r[0] for r in rows] == ["Seed"]  # Bob rolled back


def test_writer_autocommits_and_runs_exporters(db):
    calls = []
    db.register_exporter(lambda: calls.append("exported"))
    with db.writer() as conn:
        conn.execute("INSERT INTO investors(name) VALUES('Carol')")
    # committed
    fresh = sqlite3.connect(os.environ["KIMI_DB_PATH"])
    assert fresh.execute("SELECT name FROM investors").fetchall() == [("Carol",)]
    fresh.close()
    assert calls == ["exported"]


def test_writer_inside_transaction_defers_commit_and_export(db):
    calls = []
    db.register_exporter(lambda: calls.append("exported"))
    with db.transaction():
        with db.writer() as conn:
            conn.execute("INSERT INTO investors(name) VALUES('Dave')")
        assert calls == []  # not exported until the outer transaction commits
    assert calls == ["exported"]  # exactly once, after commit
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError` (no `app/db.py`).

- [ ] **Step 4: Write `app/db.py`**

```python
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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add app/db.py app/config.py tests/test_db.py
git commit -m "feat: add SQLite db module (connection, schema, transaction, exporters)"
```

---

## Task 2: `investors.py` — SQLite-backed ledger + JSON exporter

**Files:**
- Modify: `app/investors.py` (add SQLite path to `load_investors` / `save_investors`; register exporter)
- Test: `tests/test_investors_sqlite.py`

**Interfaces:**
- Consumes: `app.db` (`writer`, `get_conn`, `register_exporter`); `app.config.settings.use_sqlite`.
- Produces: `load_investors()` / `save_investors()` behave identically to today but read/write SQLite when `use_sqlite` is True; on every SQLite write, `investors.json` is re-exported via `serialize_investors`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_investors_sqlite.py`:

```python
import importlib
import json

import pytest


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.investors as investors
    importlib.reload(investors)
    yield investors, tmp_path
    db.reset_for_tests()


def test_roundtrip_returns_identical_dataclasses(sqlite_env):
    investors, _ = sqlite_env
    orig = [
        investors.Investor(
            name="Alice",
            deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-02"),
                      investors.Deposit(amount=250.0, entry_spy=520.0, date="2025-03-01")],
            withdrawals=[investors.Withdrawal(units=0.5, exit_spy=540.0,
                         cost_basis=250.0, proceeds=270.0, date="2025-06-01")],
        ),
        investors.Investor(name="Bob",
            deposits=[investors.Deposit(amount=2000.0, entry_spy=510.0, date="2025-02-01")]),
    ]
    investors.save_investors(orig)
    loaded = investors.load_investors()
    assert investors.serialize_investors(loaded) == investors.serialize_investors(orig)


def test_deposit_order_preserved(sqlite_env):
    investors, _ = sqlite_env
    inv = investors.Investor(name="Carol", deposits=[
        investors.Deposit(amount=1.0, entry_spy=100.0, date="2025-01-01"),
        investors.Deposit(amount=2.0, entry_spy=101.0, date="2025-01-02"),
        investors.Deposit(amount=3.0, entry_spy=102.0, date="2025-01-03"),
    ])
    investors.save_investors([inv])
    loaded = investors.load_investors()[0]
    assert [d.amount for d in loaded.deposits] == [1.0, 2.0, 3.0]


def test_write_exports_json_snapshot(sqlite_env):
    investors, tmp_path = sqlite_env
    investors.save_investors([investors.Investor(name="Dave",
        deposits=[investors.Deposit(amount=5.0, entry_spy=500.0, date="2025-01-01")])])
    exported = json.loads((tmp_path / "investors.json").read_text())
    assert exported["investors"][0]["name"] == "Dave"
    assert exported["investors"][0]["deposits"][0]["amount"] == 5.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_investors_sqlite.py -v`
Expected: FAIL — `load_investors` still reads JSON, so DB round-trip returns `[]`.

- [ ] **Step 3: Implement the SQLite path in `app/investors.py`**

At the top of `app/investors.py`, after the existing imports, add:

```python
from app.config import settings
```

Rename the existing bodies of `load_investors` and `save_investors` to private JSON helpers and add SQLite variants plus flag-selecting wrappers. Replace the current `load_investors` and `save_investors` definitions with:

```python
def _load_investors_json(path: Path = INVESTORS_FILE) -> list[Investor]:
    if not path.exists():
        if _REPO_FILE != path and _REPO_FILE.exists():
            try:
                path.write_text(_REPO_FILE.read_text(encoding="utf-8-sig"), encoding="utf-8")
                log.info("Migrated investors.json to %s", path)
            except Exception as exc:
                log.warning("investors.json migration failed: %s", exc)
        if not path.exists():
            return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return [
            Investor(
                name=inv["name"],
                deposits=[Deposit(**d) for d in inv["deposits"]],
                withdrawals=[Withdrawal(**w) for w in inv.get("withdrawals", [])],
            )
            for inv in data["investors"]
        ]
    except Exception as exc:
        raise ValueError(f"investors.json is malformed: {exc}") from exc


def _save_investors_json(investors: list[Investor], path: Path = INVESTORS_FILE) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(serialize_investors(investors), encoding="utf-8")
    tmp.replace(path)


def _load_investors_sqlite() -> list[Investor]:
    from app import db
    conn = db.get_conn()
    result: list[Investor] = []
    for irow in conn.execute("SELECT id, name FROM investors ORDER BY id").fetchall():
        deps = [
            Deposit(amount=d["amount"], entry_spy=d["entry_spy"], date=d["date"])
            for d in conn.execute(
                "SELECT amount, entry_spy, date FROM deposits "
                "WHERE investor_id=? ORDER BY seq", (irow["id"],)).fetchall()
        ]
        wds = [
            Withdrawal(units=w["units"], exit_spy=w["exit_spy"], cost_basis=w["cost_basis"],
                       proceeds=w["proceeds"], date=w["date"])
            for w in conn.execute(
                "SELECT units, exit_spy, cost_basis, proceeds, date FROM withdrawals "
                "WHERE investor_id=? ORDER BY seq", (irow["id"],)).fetchall()
        ]
        result.append(Investor(name=irow["name"], deposits=deps, withdrawals=wds))
    return result


def _save_investors_sqlite(investors: list[Investor]) -> None:
    from app import db
    with db.writer() as conn:
        conn.execute("DELETE FROM withdrawals")
        conn.execute("DELETE FROM deposits")
        conn.execute("DELETE FROM investors")
        for inv in investors:
            cur = conn.execute("INSERT INTO investors(name) VALUES(?)", (inv.name,))
            iid = cur.lastrowid
            for seq, d in enumerate(inv.deposits):
                conn.execute(
                    "INSERT INTO deposits(investor_id, amount, entry_spy, date, seq) "
                    "VALUES(?,?,?,?,?)", (iid, d.amount, d.entry_spy, d.date, seq))
            for seq, w in enumerate(inv.withdrawals):
                conn.execute(
                    "INSERT INTO withdrawals(investor_id, units, exit_spy, cost_basis, "
                    "proceeds, date, seq) VALUES(?,?,?,?,?,?,?)",
                    (iid, w.units, w.exit_spy, w.cost_basis, w.proceeds, w.date, seq))


def _export_investors_json() -> None:
    """Regenerate investors.json from the SQLite source of truth."""
    _save_investors_json(_load_investors_sqlite())


def load_investors(path: Path = INVESTORS_FILE) -> list[Investor]:
    if settings.use_sqlite:
        return _load_investors_sqlite()
    return _load_investors_json(path)


def save_investors(investors: list[Investor], path: Path = INVESTORS_FILE) -> None:
    if settings.use_sqlite:
        _save_investors_sqlite(investors)
    else:
        _save_investors_json(investors, path)
```

At the very bottom of `app/investors.py`, register the exporter so committed SQLite writes refresh the JSON snapshot:

```python
# Regenerate investors.json after every committed SQLite write batch.
try:
    from app import db as _db
    _db.register_exporter(_export_investors_json)
except Exception:  # pragma: no cover - db import optional in some tooling
    pass
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_investors_sqlite.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Confirm the JSON path is unchanged**

Run: `python -m pytest tests/test_investors.py tests/test_deposit.py tests/test_withdraw.py -v`
Expected: PASS (flag defaults off; existing behavior intact).

- [ ] **Step 6: Commit**

```bash
git add app/investors.py tests/test_investors_sqlite.py
git commit -m "feat: SQLite-backed investor ledger behind use_sqlite flag"
```

---

## Task 3: `pending_withdrawals.py` — SQLite-backed pending store

**Files:**
- Modify: `app/pending_withdrawals.py`
- Test: `tests/test_pending_withdrawals_sqlite.py`

**Interfaces:**
- Consumes: `app.db` (`writer`, `get_conn`, `register_exporter`); `settings.use_sqlite`.
- Produces: `save_pending_withdrawal`, `remove_pending_withdrawal`, `load_pending_withdrawals`, `get_pending_withdrawal` unchanged externally; SQLite-backed when flag on; exports `pending_withdrawals.json`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pending_withdrawals_sqlite.py`:

```python
import importlib
import json

import pytest


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("PENDING_WITHDRAWALS_PATH", str(tmp_path / "pending_withdrawals.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.pending_withdrawals as pw
    importlib.reload(pw)
    yield pw, tmp_path
    db.reset_for_tests()


def test_save_get_remove_roundtrip(sqlite_env):
    pw, _ = sqlite_env
    pw.save_pending_withdrawal("wd-1", "Alice", 500.0, "2025-06-01T00:00:00", "2025-06-02T00:00:00")
    pw.save_pending_withdrawal("wd-2", "Bob", 250.0, "2025-06-01T00:00:00", "2025-06-02T00:00:00",
                               spy_price=540.0)
    assert pw.get_pending_withdrawal("wd-1")["investor"] == "Alice"
    assert pw.get_pending_withdrawal("wd-2")["spy_price"] == 540.0
    assert "spy_price" not in pw.get_pending_withdrawal("wd-1")  # None omitted, matches JSON shape
    pw.remove_pending_withdrawal("wd-1")
    assert pw.get_pending_withdrawal("wd-1") is None
    assert {r["id"] for r in pw.load_pending_withdrawals()} == {"wd-2"}


def test_exports_json(sqlite_env):
    pw, tmp_path = sqlite_env
    pw.save_pending_withdrawal("wd-9", "Carol", 100.0, "2025-06-01T00:00:00", "2025-06-02T00:00:00")
    exported = json.loads((tmp_path / "pending_withdrawals.json").read_text())
    assert exported["pending"][0]["id"] == "wd-9"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_pending_withdrawals_sqlite.py -v`
Expected: FAIL — still JSON-backed; DB empty.

- [ ] **Step 3: Implement the SQLite path in `app/pending_withdrawals.py`**

Add near the top, after existing imports:

```python
from app.config import settings
```

Rename the current `_load`/`_save` to `_load_json`/`_save_json` (leave their bodies exactly as they are), then add the SQLite layer and flag-selecting internals. Replace the existing `_load` and `_save` names in `save_pending_withdrawal` / `remove_pending_withdrawal` / `load_pending_withdrawals` / `get_pending_withdrawal` with calls to the new `_load()` / `_save()` wrappers below:

```python
def _load_sqlite() -> list:
    from app import db
    rows = db.get_conn().execute(
        "SELECT id, investor, amount, requested_at, run_at, spy_price "
        "FROM pending_withdrawals ORDER BY rowid").fetchall()
    records = []
    for r in rows:
        rec = {
            "id": r["id"], "investor": r["investor"], "amount": r["amount"],
            "requested_at": r["requested_at"], "run_at": r["run_at"],
        }
        if r["spy_price"] is not None:
            rec["spy_price"] = r["spy_price"]
        records.append(rec)
    return records


def _save_sqlite(records: list) -> None:
    from app import db
    with db.writer() as conn:
        conn.execute("DELETE FROM pending_withdrawals")
        for rec in records:
            conn.execute(
                "INSERT INTO pending_withdrawals(id, investor, amount, requested_at, run_at, spy_price) "
                "VALUES(?,?,?,?,?,?)",
                (rec["id"], rec["investor"], rec["amount"], rec["requested_at"],
                 rec["run_at"], rec.get("spy_price")))


def _export_json() -> None:
    _save_json(_load_sqlite())


def _load() -> list:
    return _load_sqlite() if settings.use_sqlite else _load_json()


def _save(records: list) -> None:
    if settings.use_sqlite:
        _save_sqlite(records)
    else:
        _save_json(records)
```

At the bottom of the file, register the exporter:

```python
try:
    from app import db as _db
    _db.register_exporter(_export_json)
except Exception:  # pragma: no cover
    pass
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_pending_withdrawals_sqlite.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Confirm JSON path unchanged**

Run: `python -m pytest tests/test_pending_withdrawals.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/pending_withdrawals.py tests/test_pending_withdrawals_sqlite.py
git commit -m "feat: SQLite-backed pending withdrawals behind use_sqlite flag"
```

---

## Task 4: `withdrawal_audit.py` — SQLite-backed audit log (with `extra_json`)

**Files:**
- Modify: `app/withdrawal_audit.py`
- Test: `tests/test_withdrawal_audit_sqlite.py`

**Interfaces:**
- Consumes: `app.db`; `settings.use_sqlite`.
- Produces: `append_withdrawal_audit(..., **extra)` and `load_withdrawal_audit()` unchanged externally; the open-ended `**extra` kwargs are stored as JSON and restored flat on load; exports `withdrawal_audit.json`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_withdrawal_audit_sqlite.py`:

```python
import importlib
import json

import pytest


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("WITHDRAWAL_AUDIT_PATH", str(tmp_path / "withdrawal_audit.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.withdrawal_audit as wa
    importlib.reload(wa)
    yield wa, tmp_path
    db.reset_for_tests()


def test_append_preserves_extra_fields(sqlite_env):
    wa, _ = sqlite_env
    wa.append_withdrawal_audit("wd-1", "Alice", 500.0, "2025-06-01T00:00:00",
                               "2025-06-02T00:00:00", "executed",
                               completed_at="2025-06-02T00:05:00")
    wa.append_withdrawal_audit("wd-2", "Bob", 250.0, "2025-06-01T00:00:00",
                               "2025-06-02T00:00:00", "failed",
                               reason="insufficient equity")
    entries = wa.load_withdrawal_audit()
    assert entries[0]["completed_at"] == "2025-06-02T00:05:00"
    assert entries[0]["status"] == "executed"
    assert entries[1]["reason"] == "insufficient equity"
    assert "extra_json" not in entries[0]  # stored column not leaked to callers


def test_exports_json(sqlite_env):
    wa, tmp_path = sqlite_env
    wa.append_withdrawal_audit("wd-3", "Carol", 100.0, "2025-06-01T00:00:00",
                               "2025-06-02T00:00:00", "canceled",
                               canceled_at="2025-06-01T01:00:00")
    exported = json.loads((tmp_path / "withdrawal_audit.json").read_text())
    assert exported["audit"][0]["id"] == "wd-3"
    assert exported["audit"][0]["canceled_at"] == "2025-06-01T01:00:00"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_withdrawal_audit_sqlite.py -v`
Expected: FAIL — JSON-backed; DB empty.

- [ ] **Step 3: Implement the SQLite path in `app/withdrawal_audit.py`**

Add after existing imports:

```python
import json as _json_mod  # 'json' already imported; alias for clarity in extra handling
from datetime import datetime, timezone
from app.config import settings
```

Rename the current `_load`/`_save` bodies to `_load_json`/`_save_json`, then add:

```python
_CORE_FIELDS = ("id", "investor", "amount", "requested_at", "run_at", "status")


def _append_sqlite(entry: dict) -> None:
    from app import db
    core = {k: entry[k] for k in _CORE_FIELDS}
    extra = {k: v for k, v in entry.items() if k not in _CORE_FIELDS}
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO withdrawal_audit(withdrawal_id, investor, amount, requested_at, "
            "run_at, status, extra_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (core["id"], core["investor"], core["amount"], core["requested_at"],
             core["run_at"], core["status"],
             json.dumps(extra) if extra else None,
             datetime.now(timezone.utc).isoformat()))


def _load_sqlite() -> list:
    from app import db
    rows = db.get_conn().execute(
        "SELECT withdrawal_id, investor, amount, requested_at, run_at, status, extra_json "
        "FROM withdrawal_audit ORDER BY id").fetchall()
    result = []
    for r in rows:
        entry = {
            "id": r["withdrawal_id"], "investor": r["investor"], "amount": r["amount"],
            "requested_at": r["requested_at"], "run_at": r["run_at"], "status": r["status"],
        }
        if r["extra_json"]:
            entry.update(json.loads(r["extra_json"]))
        result.append(entry)
    return result


def _export_json() -> None:
    _save_json(_load_sqlite())
```

Change `append_withdrawal_audit` so that, after building `entry`, it branches on the flag instead of calling `_load`/`_save`:

```python
def append_withdrawal_audit(withdrawal_id, investor, amount, requested_at, run_at, status, **extra) -> None:
    entry = {
        "id": withdrawal_id, "investor": investor, "amount": amount,
        "requested_at": requested_at, "run_at": run_at, "status": status,
    }
    entry.update(extra)
    if settings.use_sqlite:
        _append_sqlite(entry)
    else:
        with _lock:
            records = _load_json()
            records.append(entry)
            _save_json(records)
    log.info("Recorded withdrawal audit entry %s: %s", withdrawal_id, status)
```

And make `load_withdrawal_audit` flag-aware:

```python
def load_withdrawal_audit() -> list:
    return _load_sqlite() if settings.use_sqlite else _load_json()
```

Register the exporter at the bottom of the file:

```python
try:
    from app import db as _db
    _db.register_exporter(_export_json)
except Exception:  # pragma: no cover
    pass
```

> Note: the `import json as _json_mod` alias in the imports block is not required — `json` is already imported at the top of the file; remove the alias line if your linter flags it. Keep `from datetime import datetime, timezone` and `from app.config import settings`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_withdrawal_audit_sqlite.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Confirm JSON path unchanged**

Run: `python -m pytest tests/test_withdrawal_audit.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/withdrawal_audit.py tests/test_withdrawal_audit_sqlite.py
git commit -m "feat: SQLite-backed withdrawal audit with extra_json behind use_sqlite flag"
```

---

## Task 5: `rh_deposit_log.py` — SQLite-backed RH deposit log

**Files:**
- Modify: `app/rh_deposit_log.py`
- Test: `tests/test_rh_deposit_log_sqlite.py`

**Interfaces:**
- Consumes: `app.db`; `settings.use_sqlite`.
- Produces: `load_rh_deposits`, `append_rh_deposit`, `clear_rh_deposits`, `get_rh_deposit_events` unchanged externally; SQLite-backed when flag on; exports `rh_deposits.json` (list form, sorted by date).

> The RH deposit log has no dedicated table in the four-table schema (it is intentionally a flat `date/amount` log). Store it in a fifth table `rh_deposits` added to the schema in this task.

- [ ] **Step 1: Add the table to the schema**

In `app/db.py`, append to the `_SCHEMA` string (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS rh_deposits (
    id     INTEGER PRIMARY KEY,
    date   TEXT NOT NULL,
    amount REAL NOT NULL
);
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_rh_deposit_log_sqlite.py`:

```python
import importlib
import json

import pytest


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("RH_DEPOSIT_LOG_PATH", str(tmp_path / "rh_deposits.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.rh_deposit_log as rd
    importlib.reload(rd)
    yield rd, tmp_path
    db.reset_for_tests()


def test_append_sorts_and_roundtrips(sqlite_env):
    rd, _ = sqlite_env
    rd.append_rh_deposit("2025-03-01", 300.0)
    rd.append_rh_deposit("2025-01-01", 100.0)
    rd.append_rh_deposit("2025-02-01", 200.0)
    assert rd.get_rh_deposit_events() == [
        ("2025-01-01", 100.0), ("2025-02-01", 200.0), ("2025-03-01", 300.0)]


def test_clear(sqlite_env):
    rd, _ = sqlite_env
    rd.append_rh_deposit("2025-01-01", 100.0)
    assert rd.clear_rh_deposits() == 1
    assert rd.load_rh_deposits() == []


def test_exports_json(sqlite_env):
    rd, tmp_path = sqlite_env
    rd.append_rh_deposit("2025-01-01", 100.0)
    exported = json.loads((tmp_path / "rh_deposits.json").read_text())
    assert exported == [{"date": "2025-01-01", "amount": 100.0}]
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_rh_deposit_log_sqlite.py -v`
Expected: FAIL — JSON-backed; DB empty.

- [ ] **Step 4: Implement the SQLite path in `app/rh_deposit_log.py`**

Add after existing imports:

```python
from app.config import settings
```

Rename the current `load_rh_deposits` body into `_load_json` and the write bodies accordingly, then add the SQLite layer and flag wrappers:

```python
def _load_json() -> list[RhDeposit]:
    if not _LOG_FILE.exists():
        return []
    try:
        return json.loads(_LOG_FILE.read_text())
    except Exception as exc:
        log.warning("rh_deposit_log: failed to read %s: %s", _LOG_FILE, exc)
        return []


def _write_json(deposits: list[RhDeposit]) -> None:
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(deposits, indent=2))
    tmp.replace(_LOG_FILE)


def _load_sqlite() -> list[RhDeposit]:
    from app import db
    rows = db.get_conn().execute(
        "SELECT date, amount FROM rh_deposits ORDER BY date, id").fetchall()
    return [{"date": r["date"], "amount": r["amount"]} for r in rows]


def _write_sqlite(deposits: list[RhDeposit]) -> None:
    from app import db
    with db.writer() as conn:
        conn.execute("DELETE FROM rh_deposits")
        for d in deposits:
            conn.execute("INSERT INTO rh_deposits(date, amount) VALUES(?,?)",
                         (d["date"], d["amount"]))


def _export_json() -> None:
    _write_json(_load_sqlite())


def load_rh_deposits() -> list[RhDeposit]:
    return _load_sqlite() if settings.use_sqlite else _load_json()


def append_rh_deposit(date: str, amount: float) -> None:
    deposits = load_rh_deposits()
    deposits.append({"date": date, "amount": amount})
    deposits.sort(key=lambda d: d["date"])
    if settings.use_sqlite:
        _write_sqlite(deposits)
    else:
        _write_json(deposits)


def clear_rh_deposits() -> int:
    count = len(load_rh_deposits())
    if settings.use_sqlite:
        _write_sqlite([])
    else:
        _write_json([])
    return count
```

Register the exporter at the bottom:

```python
try:
    from app import db as _db
    _db.register_exporter(_export_json)
except Exception:  # pragma: no cover
    pass
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_rh_deposit_log_sqlite.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Confirm JSON path + db schema still green**

Run: `python -m pytest tests/test_db.py tests/test_rh_deposit_log_sqlite.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/db.py app/rh_deposit_log.py tests/test_rh_deposit_log_sqlite.py
git commit -m "feat: SQLite-backed RH deposit log behind use_sqlite flag"
```

---

## Task 6: `withdrawal_execution.py` — atomic withdrawal transaction

**Files:**
- Modify: `app/withdrawal_execution.py:160-235` (the success/failure write sequence in `execute_pending_withdrawal`)
- Test: `tests/test_withdrawal_atomicity.py`

**Interfaces:**
- Consumes: `app.db.transaction`; `settings.use_sqlite`; existing `save_investors`, `remove_pending_withdrawal`, `append_withdrawal_audit`.
- Produces: when `use_sqlite` is on, the ledger write + pending removal + audit write for a withdrawal commit as one transaction; a crash between them leaves the ledger unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_withdrawal_atomicity.py`:

```python
import importlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    monkeypatch.setenv("PENDING_WITHDRAWALS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setenv("WITHDRAWAL_AUDIT_PATH", str(tmp_path / "audit.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.investors as investors
    importlib.reload(investors)
    import app.pending_withdrawals as pw
    importlib.reload(pw)
    import app.withdrawal_audit as wa
    importlib.reload(wa)
    yield db, investors, pw, wa
    db.reset_for_tests()


def test_crash_between_ledger_and_audit_rolls_back(env):
    db, investors, pw, wa = env
    # Seed a ledger with one investor holding units
    investors.save_investors([investors.Investor(name="Alice",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-01")])])
    pw.save_pending_withdrawal("wd-1", "Alice", 100.0, "2025-06-01", "2025-06-02")

    before = investors.serialize_investors(investors.load_investors())

    # Simulate the atomic block failing after the ledger write but before audit
    with pytest.raises(RuntimeError):
        with db.transaction():
            invs = investors.load_investors()
            invs[0].withdrawals.append(investors.Withdrawal(
                units=0.2, exit_spy=540.0, cost_basis=100.0, proceeds=108.0, date="2025-06-02"))
            investors.save_investors(invs)
            pw.remove_pending_withdrawal("wd-1")
            raise RuntimeError("crash before audit write")

    # Everything rolled back: ledger unchanged, pending still present, no audit entry
    assert investors.serialize_investors(investors.load_investors()) == before
    assert pw.get_pending_withdrawal("wd-1") is not None
    assert wa.load_withdrawal_audit() == []


def test_successful_transaction_commits_all_three(env):
    db, investors, pw, wa = env
    investors.save_investors([investors.Investor(name="Bob",
        deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-01")])])
    pw.save_pending_withdrawal("wd-2", "Bob", 100.0, "2025-06-01", "2025-06-02")

    with db.transaction():
        invs = investors.load_investors()
        invs[0].withdrawals.append(investors.Withdrawal(
            units=0.2, exit_spy=540.0, cost_basis=100.0, proceeds=108.0, date="2025-06-02"))
        investors.save_investors(invs)
        pw.remove_pending_withdrawal("wd-2")
        wa.append_withdrawal_audit("wd-2", "Bob", 100.0, "2025-06-01", "2025-06-02",
                                   "executed", completed_at="2025-06-02T00:05:00")

    assert len(investors.load_investors()[0].withdrawals) == 1
    assert pw.get_pending_withdrawal("wd-2") is None
    assert wa.load_withdrawal_audit()[0]["status"] == "executed"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_withdrawal_atomicity.py -v`
Expected: FAIL on `test_crash_between_ledger_and_audit_rolls_back` if the writes are not sharing one transaction (ledger would show the withdrawal / pending would be gone). It should PASS once `save_investors` and `remove_pending_withdrawal` join `db.transaction()` — which they already do via Tasks 2–4 `writer()`. Run to confirm current status.

> If both tests already pass after Tasks 2–5 (because `writer()` correctly joins the active transaction), that verifies the primitive. Proceed to wire the real execution path in Step 3 regardless.

- [ ] **Step 3: Wire the real execution path**

In `app/withdrawal_execution.py`, add to the imports:

```python
from app import db
from app.config import settings
```

Wrap the success-path writes. The current success path (after `format_withdrawal_message` and building the `Withdrawal`) runs `save_investors(investors)` inside `investors_lock`, then later `remove_pending_withdrawal` and `append_withdrawal_audit(status="executed")` outside it. Restructure so that, when `settings.use_sqlite` is True, those three writes execute inside a single `db.transaction()`.

Replace the block from the `async with investors_lock:` success branch through the executed-audit write with:

```python
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
                    if settings.use_sqlite:
                        # Atomic: ledger + pending removal + audit commit together.
                        with db.transaction():
                            save_investors(investors)
                            remove_pending_withdrawal(withdrawal_id)
                            append_withdrawal_audit(
                                withdrawal_id=record["id"], investor=record["investor"],
                                amount=record["amount"], requested_at=record["requested_at"],
                                run_at=record["run_at"], status="executed",
                                completed_at=datetime.now(_CT).isoformat(),
                            )
                        _executed_atomically = True
                    else:
                        save_investors(investors)
                        _executed_atomically = False
                except Exception as exc:
                    log.exception(
                        "execute_pending_withdrawal: atomic write failed for %s — no funds moved, marking failed",
                        withdrawal_id,
                    )
                    error_reason = f"Internal error while recording withdrawal: {exc}"
                    discord_msg = None
```

Initialize `_executed_atomically = False` just before the `async with investors_lock:` line (next to `discord_msg = None` and `error_reason = None`).

Then guard the existing post-lock cleanup so the JSON path is unchanged but the SQLite path does not double-write (pending removal and executed-audit already happened inside the transaction). Change the post-lock section to:

```python
    if not settings.use_sqlite:
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

    if not settings.use_sqlite:
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
```

> On the SQLite failure path, `remove_pending_withdrawal` is intentionally NOT called (the pending record stays so the operator can retry), and the `failed` audit entry is written via its own autocommit `writer()`. No funds moved, so no atomicity requirement there.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_withdrawal_atomicity.py tests/test_withdrawal_execution.py -v`
Expected: PASS (both new tests, and the existing execution tests with the flag off).

- [ ] **Step 5: Commit**

```bash
git add app/withdrawal_execution.py tests/test_withdrawal_atomicity.py
git commit -m "feat: atomic withdrawal write (ledger+pending+audit) under SQLite"
```

---

## Task 7: `scripts/migrate_to_sqlite.py` — one-way migrate + verify

**Files:**
- Create: `scripts/migrate_to_sqlite.py`
- Test: `tests/test_migrate_to_sqlite.py`

**Interfaces:**
- Consumes: JSON loaders (`load_investors`, `load_pending_withdrawals`, `load_withdrawal_audit`, `load_rh_deposits`) with the flag OFF; SQLite savers with the flag ON; `app.db`.
- Produces:
  - `migrate() -> dict` — copies pre-migration JSON to `/data/backup_pre_sqlite/`, loads all four JSON stores, writes them to SQLite, returns counts.
  - `verify_migration() -> list[str]` — returns a list of mismatch strings (empty = verified). Compares structure, every field, and computed figures (via `compute_breakdown`) to the cent.
  - `main()` — runs migrate then verify; prints report; exits non-zero on any mismatch.

- [ ] **Step 1: Write the failing test**

Create `tests/test_migrate_to_sqlite.py`:

```python
import importlib
import json

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # JSON sources (flag OFF for reads), DB target
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    monkeypatch.setenv("PENDING_WITHDRAWALS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setenv("WITHDRAWAL_AUDIT_PATH", str(tmp_path / "audit.json"))
    monkeypatch.setenv("RH_DEPOSIT_LOG_PATH", str(tmp_path / "rh_deposits.json"))
    monkeypatch.setenv("PRE_SQLITE_BACKUP_DIR", str(tmp_path / "backup_pre_sqlite"))
    import app.config as config
    importlib.reload(config)
    yield config, tmp_path
    import app.db as db
    db.reset_for_tests()


def _seed_json(tmp_path):
    (tmp_path / "investors.json").write_text(json.dumps({"investors": [
        {"name": "Alice", "deposits": [
            {"amount": 1000.0, "entry_spy": 500.0, "date": "2025-01-01"},
            {"amount": 500.0, "entry_spy": 520.0, "date": "2025-03-01"}],
         "withdrawals": [
            {"units": 0.4, "exit_spy": 540.0, "cost_basis": 200.0,
             "proceeds": 216.0, "date": "2025-06-01"}]},
        {"name": "Bob", "deposits": [
            {"amount": 2000.0, "entry_spy": 510.0, "date": "2025-02-01"}],
         "withdrawals": []},
    ]}))
    (tmp_path / "pending.json").write_text(json.dumps({"pending": [
        {"id": "wd-1", "investor": "Alice", "amount": 100.0,
         "requested_at": "2025-06-10", "run_at": "2025-06-11", "spy_price": 545.0}]}))
    (tmp_path / "audit.json").write_text(json.dumps({"audit": [
        {"id": "wd-0", "investor": "Alice", "amount": 216.0, "requested_at": "2025-05-31",
         "run_at": "2025-06-01", "status": "executed", "completed_at": "2025-06-01T00:05:00"}]}))
    (tmp_path / "rh_deposits.json").write_text(json.dumps([
        {"date": "2025-01-01", "amount": 1000.0}]))


def test_migrate_then_verify_passes(env):
    config, tmp_path = env
    _seed_json(tmp_path)
    import scripts.migrate_to_sqlite as m
    importlib.reload(m)
    counts = m.migrate()
    assert counts["investors"] == 2
    assert counts["deposits"] == 3
    assert counts["withdrawals"] == 1
    assert counts["pending"] == 1
    assert counts["audit"] == 1
    assert counts["rh_deposits"] == 1
    mismatches = m.verify_migration()
    assert mismatches == [], f"unexpected mismatches: {mismatches}"


def test_backup_copy_created(env):
    config, tmp_path = env
    _seed_json(tmp_path)
    import scripts.migrate_to_sqlite as m
    importlib.reload(m)
    m.migrate()
    backup = tmp_path / "backup_pre_sqlite" / "investors.json"
    assert backup.exists()
    assert json.loads(backup.read_text())["investors"][0]["name"] == "Alice"


def test_verify_detects_corruption(env):
    config, tmp_path = env
    _seed_json(tmp_path)
    import app.db as db
    importlib.reload(db)
    import scripts.migrate_to_sqlite as m
    importlib.reload(m)
    m.migrate()
    # Corrupt the DB: change Alice's deposit amount
    db.get_conn().execute("UPDATE deposits SET amount = amount + 1 WHERE id=1")
    db.get_conn().commit()
    mismatches = m.verify_migration()
    assert mismatches != []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_migrate_to_sqlite.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.migrate_to_sqlite`.

- [ ] **Step 3: Create `scripts/__init__.py` (if missing) and the script**

Ensure `scripts/__init__.py` exists (empty file) so the test can import it:

```bash
test -f scripts/__init__.py || touch scripts/__init__.py
```

Create `scripts/migrate_to_sqlite.py`:

```python
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
        print("❌ VERIFICATION FAILED:")
        for m in mismatches:
            print("   -", m)
        return 1
    print(f"✅ VERIFIED — investors={counts['investors']} "
          f"deposits={counts['deposits']} withdrawals={counts['withdrawals']} "
          f"(pending={counts['pending']} audit={counts['audit']} rh_deposits={counts['rh_deposits']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_migrate_to_sqlite.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/migrate_to_sqlite.py tests/test_migrate_to_sqlite.py
git commit -m "feat: offline JSON->SQLite migration script with exhaustive verification"
```

---

## Task 8: `backup.py` — include `kimi.db` in the Gist

**Files:**
- Modify: `app/backup.py`
- Test: `tests/test_backup_db.py`

**Interfaces:**
- Consumes: `settings.use_sqlite`; `KIMI_DB_PATH`.
- Produces: when `use_sqlite` is on and `kimi.db` exists, the Gist payload includes `kimi.db.base64` alongside the existing JSON files.

- [ ] **Step 1: Write the failing test**

Create `tests/test_backup_db.py`:

```python
import base64
import importlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    (tmp_path / "kimi.db").write_bytes(b"SQLITE_FAKE_BINARY_\x00\x01\x02")
    import app.backup as backup
    importlib.reload(backup)
    yield backup, tmp_path


@pytest.mark.asyncio
async def test_db_included_in_payload(env, monkeypatch):
    backup, tmp_path = env
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def patch(self, url, json, headers):
            captured["files"] = json["files"]
            return FakeResp()

    monkeypatch.setattr(backup.settings, "github_gist_token", "t")
    monkeypatch.setattr(backup.settings, "github_gist_id", "g")
    monkeypatch.setattr(backup.httpx, "AsyncClient", FakeClient)

    result = await backup.push_backup()
    assert result["ok"] is True
    assert "kimi.db.base64" in captured["files"]
    decoded = base64.b64decode(captured["files"]["kimi.db.base64"]["content"])
    assert decoded == b"SQLITE_FAKE_BINARY_\x00\x01\x02"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_backup_db.py -v`
Expected: FAIL — `kimi.db.base64` not present.

- [ ] **Step 3: Implement in `app/backup.py`**

Add to the imports at the top:

```python
import base64
from app.config import settings
```

(`settings` is already imported — keep a single import.) Inside `push_backup`, after the loop that fills `files` from `_BACKUP_FILES` and before the `if not files:` check, add:

```python
    # Include the SQLite source-of-truth DB (binary → base64) when enabled.
    if settings.use_sqlite:
        db_path = Path(os.getenv("KIMI_DB_PATH", "/data/kimi.db"))
        if db_path.exists():
            try:
                files["kimi.db.base64"] = {
                    "content": base64.b64encode(db_path.read_bytes()).decode("ascii")
                }
            except Exception as exc:
                log.warning("Backup: could not read %s: %s", db_path, exc)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_backup_db.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/backup.py tests/test_backup_db.py
git commit -m "feat: include base64 kimi.db in Gist backup when SQLite enabled"
```

---

## Task 9: Cross-backend parity + full-suite gate

**Files:**
- Create: `tests/test_backend_parity.py`

**Interfaces:**
- Consumes: all modules under both flag values.
- Produces: a test proving `compute_breakdown` and the withdrawal message render identically whether the ledger came from JSON or SQLite, plus a green full suite.

- [ ] **Step 1: Write the parity test**

Create `tests/test_backend_parity.py`:

```python
import importlib

import pytest


def _fixture_investors(investors_mod):
    return [
        investors_mod.Investor(name="Alice",
            deposits=[investors_mod.Deposit(1000.0, 500.0, "2025-01-01"),
                      investors_mod.Deposit(500.0, 520.0, "2025-03-01")],
            withdrawals=[investors_mod.Withdrawal(0.4, 540.0, 200.0, 216.0, "2025-06-01")]),
        investors_mod.Investor(name="Bob",
            deposits=[investors_mod.Deposit(2000.0, 510.0, "2025-02-01")]),
    ]


def test_breakdown_identical_across_backends(tmp_path, monkeypatch):
    # JSON backend
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    import app.config as config
    importlib.reload(config)
    monkeypatch.setattr(config.settings, "use_sqlite", False)
    import app.investors as inv_json
    importlib.reload(inv_json)
    data = _fixture_investors(inv_json)
    inv_json.save_investors(data)
    json_break = inv_json.format_discord_message(
        inv_json.compute_breakdown(inv_json.load_investors(), 500.0, 1_000_000.0), "test")

    # SQLite backend
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setattr(config.settings, "use_sqlite", True)
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    db.init_schema()
    import app.investors as inv_sql
    importlib.reload(inv_sql)
    inv_sql.save_investors(_fixture_investors(inv_sql))
    sql_break = inv_sql.format_discord_message(
        inv_sql.compute_breakdown(inv_sql.load_investors(), 500.0, 1_000_000.0), "test")

    assert json_break == sql_break
    db.reset_for_tests()
```

- [ ] **Step 2: Run the parity test**

Run: `python -m pytest tests/test_backend_parity.py -v`
Expected: PASS.

- [ ] **Step 3: Run the FULL suite with the flag OFF (default production state)**

Run: `python -m pytest -q`
Expected: PASS — every existing test green; the flag defaults off so nothing regressed.

- [ ] **Step 4: Run the SQLite-specific suite explicitly**

Run: `python -m pytest tests/test_db.py tests/test_investors_sqlite.py tests/test_pending_withdrawals_sqlite.py tests/test_withdrawal_audit_sqlite.py tests/test_rh_deposit_log_sqlite.py tests/test_withdrawal_atomicity.py tests/test_migrate_to_sqlite.py tests/test_backup_db.py tests/test_backend_parity.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_backend_parity.py
git commit -m "test: cross-backend parity for investor breakdown rendering"
```

---

## Task 10: Operator runbook

**Files:**
- Create: `docs/runbooks/sqlite-cutover.md`

**Interfaces:**
- Consumes: nothing (documentation).
- Produces: the exact cutover and rollback steps for the operator.

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/sqlite-cutover.md`:

```markdown
# Investor Ledger SQLite — Cutover & Rollback Runbook

## Preconditions
- Branch `feature/investor-ledger-sqlite` deployed to Render with `USE_SQLITE=false`.
- Confirm the app is healthy (`GET /healthz`) and behaving normally on JSON.

## Cutover
1. Take a manual Gist backup: `POST /run-backup {"secret": "<WEBHOOK_SECRET>"}`.
2. Open the Render shell and run the migration (flag still off for the read side):
   `python -m scripts.migrate_to_sqlite`
3. Confirm the final line is `✅ VERIFIED — investors=… deposits=… withdrawals=…`.
   If it prints `❌ VERIFICATION FAILED`, STOP — do not flip the flag; the JSON
   files are untouched and the app is still running on them.
4. Set Render env `USE_SQLITE=true` and redeploy.
5. Smoke test: `GET /public-stats`, run `/run-report {"report":"investors"}` and
   confirm the Investor Tracker Discord message looks identical to before.

## Rollback (instant, lossless)
1. Set Render env `USE_SQLITE=false` and redeploy.
2. The app resumes on the JSON files, which the SQLite path kept current on every
   write. The frozen pre-migration copy is in `/data/backup_pre_sqlite/` if needed.

## Notes
- `/data/kimi.db` is the source of truth while `USE_SQLITE=true`; the JSON files are
  regenerated after every committed write and are safe to read but not to hand-edit.
- Backups now include `kimi.db.base64` in the Gist alongside the JSON snapshots.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/sqlite-cutover.md
git commit -m "docs: SQLite cutover and rollback runbook"
```

---

## Self-Review Notes (author)

- **Spec coverage:** §4.2 db.py → Task 1; §4.3 schema → Task 1 (+ rh_deposits table Task 5); §4.4 unchanged domain/signatures → Tasks 2–5 keep signatures, parity in Task 9; §4.5 atomic withdrawal → Task 6; §4.6 flag → Task 1 config; §5 migrate+verify → Task 7; §6 rollback → Task 10 runbook; §7 backups → Task 8; §8 testing → every task + Task 9. All spec sections mapped.
- **Ordering guarantee:** `seq` columns (deposits/withdrawals) set + read `ORDER BY seq`; covered by `test_deposit_order_preserved`.
- **Extra-field fidelity:** audit `extra_json` round-trips; covered by `test_append_preserves_extra_fields`.
- **Flag-off safety:** existing suites re-run at Tasks 2–6 and full suite at Task 9 Step 3.
- **Type consistency:** `writer()`/`transaction()`/`register_exporter`/`get_conn`/`init_schema`/`reset_for_tests` names identical across all tasks; `_save_sqlite`/`_append_sqlite`/`_write_sqlite` referenced by Task 7 match their defining tasks (3/4/5).
```