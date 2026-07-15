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
