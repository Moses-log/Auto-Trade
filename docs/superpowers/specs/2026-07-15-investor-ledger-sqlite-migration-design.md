# Investor Ledger → SQLite Migration — Design Spec

**Date:** 2026-07-15
**Status:** Approved for planning (pending final spec review)
**Scope:** Investor ledger only (`investors.json`, `pending_withdrawals.json`, `withdrawal_audit.json`, `rh_deposits.json`)

---

## 1. Problem

The Kimi API stores investor-ledger state as four separate JSON files on the Render disk.
Each file is written atomically on its own (`tmp` + `replace`), but there is **no transaction
spanning the files**. The withdrawal execution path (`execute_pending_withdrawal`) performs
three sequential, independent writes:

1. `save_investors(investors)` — records the withdrawal in the ledger (funds move)
2. `remove_pending_withdrawal(id)` — clears the in-flight request
3. `append_withdrawal_audit(...)` — writes the audit record

If the process dies between (1) and (3), the ledger and audit log disagree. The code itself
documents this hazard:

> `withdrawal %s WAS EXECUTED (funds moved) but audit write failed — manual reconciliation needed`
> — `app/withdrawal_execution.py:228`

Locking is also inconsistent across these modules: `investors.py` uses `asyncio.Lock`,
`pending_withdrawals.py` and `withdrawal_audit.py` use `threading.Lock`, and
`rh_deposit_log.py` uses no lock at all. Recovery from any inconsistency currently relies on
the **nightly** Gist backup — up to ~24h of potential loss.

This is the highest-value integrity fix because it sits on the real-money, investor-facing path
(the hedge-fund layer).

## 2. Goals

- Make the multi-file withdrawal write **all-or-nothing** (atomic).
- Guarantee **zero data change** during migration — every value and every computed financial
  figure identical before and after, verified programmatically to the cent.
- Preserve **all existing behavior**: identical Discord messages, identical reports, identical
  API responses, unchanged domain math.
- Keep the change **reversible at any time** with no data loss.
- Keep the existing Gist backup working (human-readable JSON) and add a binary DB copy.

## 3. Non-Goals

- Migrating the other 14 `/data/` JSON files (trade records, caches, visits, etc.). Out of scope
  for this first migration; may follow once SQLite is trusted.
- Rewriting or "improving" any domain math (`compute_nav_per_unit`, `compute_withdrawal_lots`,
  `compute_time_weighted_capital`, FIFO tax-lot logic). Untouched.
- Introducing an external database service. SQLite stays a single file on the existing disk.
- Any change to Discord message formatting, report content, or public endpoints.

## 4. Architecture

### 4.1 Source of truth flips; JSON becomes derived exports

SQLite (`/data/kimi.db`) becomes the source of truth. After every successful write, the code
**exports** the equivalent JSON files (`investors.json`, `pending_withdrawals.json`,
`withdrawal_audit.json`, `rh_deposits.json`) as read-only snapshots. Rationale:

- The existing **Gist backup keeps working unchanged** — it still finds human-readable JSON to push.
- The JSON exports stay continuously current, enabling **instant, lossless rollback** to the JSON
  backend and reconstruction if SQLite is ever abandoned.
- The operator can still read the ledger with plain eyes.

### 4.2 New module: `app/db.py`

Owns the single SQLite connection and exposes the transaction primitive.

- Opens `/data/kimi.db` (path overridable via `KIMI_DB_PATH` env for tests).
- Enables `PRAGMA journal_mode=WAL` (durability + safe concurrent reads) and
  `PRAGMA foreign_keys=ON`.
- Exposes a `transaction()` context manager: `BEGIN` on enter, `COMMIT` on clean exit,
  `ROLLBACK` on any exception. Reused connection; a module-level lock serialises writers.
- Schema created idempotently on first import (`CREATE TABLE IF NOT EXISTS`).

### 4.3 Schema (normalized; DB enforces correctness)

```sql
CREATE TABLE investors (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE deposits (
    id           INTEGER PRIMARY KEY,
    investor_id  INTEGER NOT NULL REFERENCES investors(id),
    amount       REAL NOT NULL CHECK (amount >= 0),
    entry_spy    REAL NOT NULL,
    date         TEXT NOT NULL,
    seq          INTEGER NOT NULL          -- preserves original deposit order (FIFO)
);

CREATE TABLE withdrawals (
    id           INTEGER PRIMARY KEY,
    investor_id  INTEGER NOT NULL REFERENCES investors(id),
    units        REAL NOT NULL,
    exit_spy     REAL NOT NULL,
    cost_basis   REAL NOT NULL,
    proceeds     REAL NOT NULL,
    date         TEXT NOT NULL,
    seq          INTEGER NOT NULL          -- preserves original withdrawal order
);

CREATE TABLE pending_withdrawals (
    id            TEXT PRIMARY KEY,
    investor      TEXT NOT NULL,
    amount        REAL NOT NULL CHECK (amount >= 0),
    requested_at  TEXT NOT NULL,
    run_at        TEXT NOT NULL,
    spy_price     REAL                     -- nullable (only stored if user locked a price)
);

CREATE TABLE withdrawal_audit (
    id            INTEGER PRIMARY KEY,
    withdrawal_id TEXT NOT NULL,
    investor      TEXT NOT NULL,
    amount        REAL NOT NULL,
    requested_at  TEXT NOT NULL,
    run_at        TEXT NOT NULL,
    status        TEXT NOT NULL,
    extra_json    TEXT,                     -- **extra kwargs (completed_at, reason, ...) as JSON
    created_at    TEXT NOT NULL
);
```

`seq` columns preserve list order exactly, because the FIFO tax-lot logic depends on deposit and
withdrawal ordering. `extra_json` captures the open-ended `**extra` kwargs that
`append_withdrawal_audit` accepts, so no audit field is lost.

### 4.4 Domain layer unchanged

`load_investors()` still returns `list[Investor]` (same `Investor`/`Deposit`/`Withdrawal`
dataclasses); only its internals change from JSON parsing to row reading. `save_investors()`,
`save_pending_withdrawal()`, `remove_pending_withdrawal()`, `append_withdrawal_audit()`,
`get_pending_withdrawal()`, `load_withdrawal_audit()`, `load_rh_deposits()`,
`append_rh_deposit()` keep their signatures. Every downstream consumer (`discord_commands.py`,
`main.py`, `pnl.py`, `scheduler.py`, `tax.py`, `withdrawal_execution.py`) is oblivious.

### 4.5 The withdrawal path becomes atomic

`execute_pending_withdrawal` wraps the three writes in one transaction:

```python
with db.transaction():
    save_investors(investors)         # write via the shared transaction
    remove_pending_withdrawal(id)
    append_withdrawal_audit(...)
# JSON snapshots exported once, only after COMMIT succeeds
```

Process death anywhere inside the block rolls the whole thing back. "Funds moved but no audit
record" becomes structurally impossible. The `transaction()`-aware variants of the write
functions accept the active connection so all three writes share one atomic unit.

### 4.6 Backend selection

A `USE_SQLITE` env flag (default **false**) selects the backend in each affected module.
`false` = current pure-JSON behavior (no change). `true` = SQLite source of truth with JSON export.

## 5. Data Transfer & Verification

One-way, offline, operator-triggered script: `scripts/migrate_to_sqlite.py`. Never runs on deploy.

### 5.1 Transfer
Reads current JSON via the **existing loaders** (not a bespoke parser) and inserts all records
into a fresh `kimi.db` inside a single transaction. On any failure the DB is discarded; JSON is
never modified. Original JSON is first copied to `/data/backup_pre_sqlite/`.

### 5.2 Verification (`verify_migration()`) — abort unless all pass
Reads back out of SQLite and asserts identity vs JSON:

- **Structural counts:** investor count; per investor: deposit / withdrawal counts; pending count;
  audit-entry count.
- **Every field of every row:** deposits (`amount`, `entry_spy`, `date`, order); withdrawals
  (`units`, `exit_spy`, `cost_basis`, `proceeds`, `date`, order); pending records (all fields incl.
  nullable `spy_price`); audit entries (all fields incl. every `extra` key).
- **Computed financial figures**, run through the real `investors.py` functions against a fixed
  synthetic equity constant so both sides compute identically: per-investor net units, cost basis,
  current equity, total withdrawn; fund NAV-per-unit; total portfolio; full `compute_breakdown()`.

Money compared to the cent (rounded to kill float noise); everything else exact. Prints a
line-by-line report ending in `✅ VERIFIED — N investors / N deposits / N withdrawals` or a loud
`❌` at the first mismatch. **No green check → no cutover.**

### 5.3 Cutover
1. Deploy code with `USE_SQLITE=false` → zero behavior change; confirm nothing broke.
2. Run `scripts/migrate_to_sqlite.py` → obtain `✅ VERIFIED`.
3. Set `USE_SQLITE=true`, redeploy → SQLite is source of truth; JSON exported after each write.

## 6. Rollback

Set `USE_SQLITE=false` and redeploy. Because the SQLite path exports JSON on every write, the JSON
files are continuously current — the app resumes on JSON with zero data loss. The frozen
pre-migration copy in `/data/backup_pre_sqlite/` remains as an independent safety net.

## 7. Backups

`backup.py` is unchanged for the JSON exports (still pushed to the Gist) **and** additionally
uploads `kimi.db` base64-encoded to the same Gist, giving both a readable snapshot and an exact
binary copy.

## 8. Testing (TDD — tests written first)

- `db.py`: transaction commits persist; **mid-transaction exception rolls back, DB untouched**
  (the core guarantee, tested explicitly).
- Round-trip: `save_investors → load_investors` through SQLite returns identical dataclasses.
- Round-trip for pending withdrawals, audit entries (incl. `extra` fields), and rh deposits.
- Migration + verification against a fixture ledger; plus a **deliberately corrupted DB** proving
  `verify_migration()` fails loudly.
- **Simulated crash** between ledger write and audit write asserts rollback left the ledger
  unchanged (the bug-that-can't-happen-anymore test).
- **Report parity:** `compute_breakdown()` / withdrawal message output identical across both
  backends on the same fixture (guards Discord/report content).
- Existing withdrawal / investor / deposit / tax tests re-run green against **both** backends.

## 9. Affected Files

- **New:** `app/db.py`, `scripts/migrate_to_sqlite.py`, tests under `tests/`.
- **Modified (internals only, signatures preserved):** `investors.py`, `pending_withdrawals.py`,
  `withdrawal_audit.py`, `rh_deposit_log.py`, `withdrawal_execution.py` (transaction wrap),
  `backup.py` (add `.db`), `config.py` (`USE_SQLITE`, `KIMI_DB_PATH`).
- **Unchanged:** all domain math, all Discord/report formatting, all public endpoints, all other
  `/data/` files.

## 10. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Data corruption during transfer | Offline script; exhaustive `verify_migration()` gate; original JSON copied aside and never mutated. |
| Subtle float drift | Both stores use IEEE-754 doubles; money compared to the cent; parity test on computed figures. |
| Behavior regression | `USE_SQLITE` defaults off; both backends tested; instant redeploy rollback. |
| Backup gap for binary DB | JSON exports still pushed; `.db` added base64 to the same Gist. |
| FIFO order loss | Explicit `seq` columns preserve deposit/withdrawal ordering; verified in tests. |
