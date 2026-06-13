import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from unittest.mock import patch

import pytest


def test_get_snapshots_returns_empty_list_when_file_missing(tmp_path):
    from app.rh_equity_history import get_snapshots
    with patch("app.rh_equity_history._RECORD_FILE", tmp_path / "missing.json"):
        assert get_snapshots() == []


@pytest.mark.asyncio
async def test_record_snapshot_creates_new_entry(tmp_path):
    from app.rh_equity_history import get_snapshots, record_snapshot
    record_file = tmp_path / "rh_equity_history.json"
    with patch("app.rh_equity_history._RECORD_FILE", record_file):
        await record_snapshot("2026-06-12", 1749734400, 10100.0, 603.0)
        snapshots = get_snapshots()

    assert snapshots == [
        {"date": "2026-06-12", "ts": 1749734400, "equity": 10100.0, "spy_close": 603.0}
    ]


@pytest.mark.asyncio
async def test_record_snapshot_upserts_by_date(tmp_path):
    """Recording twice for the same ET calendar date replaces the prior
    entry rather than appending a duplicate."""
    from app.rh_equity_history import get_snapshots, record_snapshot
    record_file = tmp_path / "rh_equity_history.json"
    with patch("app.rh_equity_history._RECORD_FILE", record_file):
        await record_snapshot("2026-06-12", 1749734400, 10100.0, 603.0)
        await record_snapshot("2026-06-12", 1749738000, 10150.0, 604.0)
        snapshots = get_snapshots()

    assert len(snapshots) == 1
    assert snapshots[0]["equity"] == 10150.0
    assert snapshots[0]["spy_close"] == 604.0


@pytest.mark.asyncio
async def test_get_snapshots_sorted_oldest_first(tmp_path):
    from app.rh_equity_history import get_snapshots, record_snapshot
    record_file = tmp_path / "rh_equity_history.json"
    with patch("app.rh_equity_history._RECORD_FILE", record_file):
        await record_snapshot("2026-06-12", 1749734400, 10200.0, 606.0)
        await record_snapshot("2026-06-11", 1749648000, 10000.0, 600.0)
        snapshots = get_snapshots()

    assert [s["date"] for s in snapshots] == ["2026-06-11", "2026-06-12"]


def test_get_snapshots_handles_malformed_json(tmp_path):
    from app.rh_equity_history import get_snapshots
    record_file = tmp_path / "rh_equity_history.json"
    record_file.write_text("not valid json")
    with patch("app.rh_equity_history._RECORD_FILE", record_file):
        assert get_snapshots() == []
