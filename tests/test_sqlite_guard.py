"""Tests for the SQLite startup readiness guard (app/sqlite_guard.py).

The guard runs once at startup. When USE_SQLITE is on it creates the schema and
guards against the "flag flipped before migration" state: if the SQLite ledger
is empty while the JSON ledger still has investors, it falls back to JSON for
this process (no clobber) and fires a CRITICAL alert. Fixtures deliberately do
NOT reload app.config (that would replace the settings singleton and pollute
other tests) — they monkeypatch use_sqlite on the shared singleton.
"""
import importlib

import pytest


@pytest.fixture()
def guard_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_DB_PATH", str(tmp_path / "kimi.db"))
    monkeypatch.setenv("INVESTORS_PATH", str(tmp_path / "investors.json"))
    from app.config import settings
    import app.db as db
    importlib.reload(db)
    db.reset_for_tests()
    import app.investors as investors
    importlib.reload(investors)
    import app.sqlite_guard as guard
    importlib.reload(guard)
    yield guard, db, investors, settings, tmp_path
    db.reset_for_tests()


class _Recorder:
    """Async notifier stand-in that records the messages it was sent."""
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_flag_off_is_noop(guard_env):
    guard, db, investors, settings, _ = guard_env
    monkey_off = False
    settings.use_sqlite = monkey_off  # explicit: flag off
    rec = _Recorder()
    used_sqlite = await guard.ensure_sqlite_ready(notifier=rec)
    assert used_sqlite is False
    assert rec.messages == []
    assert settings.use_sqlite is False


@pytest.mark.asyncio
async def test_migrated_db_stays_on_sqlite(guard_env):
    guard, db, investors, settings, _ = guard_env
    settings.use_sqlite = True
    db.init_schema()
    investors.save_investors([
        investors.Investor(name="Alice",
            deposits=[investors.Deposit(amount=1000.0, entry_spy=500.0, date="2025-01-01")]),
    ])
    rec = _Recorder()
    used_sqlite = await guard.ensure_sqlite_ready(notifier=rec)
    assert used_sqlite is True
    assert settings.use_sqlite is True
    assert rec.messages == []


@pytest.mark.asyncio
async def test_empty_db_but_json_has_data_falls_back_and_alerts(guard_env):
    guard, db, investors, settings, tmp_path = guard_env
    # JSON ledger has investors (write it via the JSON path, flag off)
    settings.use_sqlite = False
    investors.save_investors([
        investors.Investor(name="Bob",
            deposits=[investors.Deposit(amount=2000.0, entry_spy=510.0, date="2025-02-01")]),
    ])
    assert (tmp_path / "investors.json").exists()
    # Now the operator flips the flag on WITHOUT migrating: SQLite is empty.
    settings.use_sqlite = True
    rec = _Recorder()
    used_sqlite = await guard.ensure_sqlite_ready(notifier=rec)
    assert used_sqlite is False                 # fell back
    assert settings.use_sqlite is False         # flipped off for this process
    assert len(rec.messages) == 1               # exactly one alert
    assert "migrate" in rec.messages[0].lower()


@pytest.mark.asyncio
async def test_fresh_system_both_empty_uses_sqlite(guard_env, monkeypatch):
    guard, db, investors, settings, tmp_path = guard_env
    # Neutralize the repo-file seed fallback in _load_investors_json so the JSON
    # ledger is genuinely empty (otherwise it seeds from the committed repo file).
    monkeypatch.setattr(investors, "_REPO_FILE", tmp_path / "nonexistent_repo.json")
    settings.use_sqlite = True
    # No JSON file, no SQLite data — a genuinely fresh system.
    rec = _Recorder()
    used_sqlite = await guard.ensure_sqlite_ready(notifier=rec)
    assert used_sqlite is True
    assert settings.use_sqlite is True
    assert rec.messages == []


@pytest.mark.asyncio
async def test_unexpected_error_falls_back_and_alerts(guard_env, monkeypatch):
    guard, db, investors, settings, _ = guard_env
    settings.use_sqlite = True
    # Force an unexpected failure inside the readiness check.
    monkeypatch.setattr(db, "init_schema", lambda: (_ for _ in ()).throw(RuntimeError("disk gone")))
    rec = _Recorder()
    used_sqlite = await guard.ensure_sqlite_ready(notifier=rec)
    assert used_sqlite is False              # never crashes startup
    assert settings.use_sqlite is False      # fell back to JSON
    assert len(rec.messages) == 1            # alerted


@pytest.mark.asyncio
async def test_creates_schema_when_db_absent(guard_env):
    guard, db, investors, settings, tmp_path = guard_env
    settings.use_sqlite = True
    assert not (tmp_path / "kimi.db").exists()
    rec = _Recorder()
    await guard.ensure_sqlite_ready(notifier=rec)
    # Tables now exist — a ledger read no longer raises "no such table".
    names = {r[0] for r in db.get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "investors" in names
