import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

import json
from unittest.mock import patch


def test_append_and_load_round_trip(tmp_path):
    log_path = tmp_path / "claude_inspection_log.json"
    with patch("app.claude_inspection._INSPECTION_LOG_PATH", str(log_path)):
        from app.claude_inspection import _append_inspection_log, _load_recent_inspection_entries

        _append_inspection_log({"timestamp": "2026-07-06T09:35:00", "status": "completed"})
        _append_inspection_log({"timestamp": "2026-07-13T09:35:00", "status": "no_changes"})

        entries = _load_recent_inspection_entries()
        assert len(entries) == 2
        assert entries[-1]["status"] == "no_changes"


def test_load_returns_empty_list_when_file_missing(tmp_path):
    log_path = tmp_path / "does_not_exist.json"
    with patch("app.claude_inspection._INSPECTION_LOG_PATH", str(log_path)):
        from app.claude_inspection import _load_recent_inspection_entries
        assert _load_recent_inspection_entries() == []


def test_load_respects_limit(tmp_path):
    log_path = tmp_path / "claude_inspection_log.json"
    with patch("app.claude_inspection._INSPECTION_LOG_PATH", str(log_path)):
        from app.claude_inspection import _append_inspection_log, _load_recent_inspection_entries
        for i in range(10):
            _append_inspection_log({"timestamp": f"2026-0{i%9+1}-01T09:35:00", "status": "completed"})
        entries = _load_recent_inspection_entries(limit=3)
        assert len(entries) == 3


def test_append_caps_history_at_36_entries(tmp_path):
    log_path = tmp_path / "claude_inspection_log.json"
    with patch("app.claude_inspection._INSPECTION_LOG_PATH", str(log_path)):
        from app.claude_inspection import _append_inspection_log
        for i in range(40):
            _append_inspection_log({"timestamp": f"entry-{i}", "status": "completed"})
        with open(log_path) as f:
            records = json.load(f)
        assert len(records) == 36
        assert records[-1]["timestamp"] == "entry-39"
