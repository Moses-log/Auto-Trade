import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — see Task 6's note on the
# test_config_rh.py alphabetical-collision regression found in Task 5.

import json
from unittest.mock import patch


def test_history_string_includes_recent_inspection_activity(tmp_path):
    rebalance_log = tmp_path / "claude_rebalance_log.json"
    rebalance_log.write_text(json.dumps([{
        "timestamp": "2026-07-01T09:35:00", "status": "completed",
        "portfolio_value": 10000.0, "trades_executed": [], "analysis_body": "",
    }]))

    inspection_entries = [{
        "timestamp": "2026-07-13T09:35:00", "status": "completed",
        "trades_executed": [{"action": "SELL", "ticker": "NOW"}],
        "notes": {"NOW": "Guidance cut on Jul 10 earnings call — closed position."},
    }]

    with patch("app.claude_manager._LOG_PATH", str(rebalance_log)), \
         patch("app.claude_inspection._load_recent_inspection_entries", return_value=inspection_entries):
        from app.claude_manager import _load_recent_history
        _, history_str = _load_recent_history()

    assert "Guidance cut on Jul 10" in history_str
    assert "SELL" in history_str and "NOW" in history_str


def test_history_string_unchanged_when_no_inspection_activity(tmp_path):
    rebalance_log = tmp_path / "claude_rebalance_log.json"
    rebalance_log.write_text(json.dumps([{
        "timestamp": "2026-07-01T09:35:00", "status": "completed",
        "portfolio_value": 10000.0, "trades_executed": [], "analysis_body": "",
    }]))

    with patch("app.claude_manager._LOG_PATH", str(rebalance_log)), \
         patch("app.claude_inspection._load_recent_inspection_entries", return_value=[]):
        from app.claude_manager import _load_recent_history
        records, history_str = _load_recent_history()

    assert len(records) == 1
    assert "Inspection" not in history_str
