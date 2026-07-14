# tests/test_decision_review.py
import json
import os
from datetime import date

from app.decision_review import Decision, load_executed_decisions, MAX_DECISIONS


def _write_logs(tmp_path, rebalance_entries, inspection_entries):
    reb = tmp_path / "reb.json"; insp = tmp_path / "insp.json"
    reb.write_text(json.dumps(rebalance_entries)); insp.write_text(json.dumps(inspection_entries))
    os.environ["CLAUDE_REBALANCE_LOG_PATH"] = str(reb)
    os.environ["CLAUDE_INSPECTION_LOG_PATH"] = str(insp)


def test_extracts_only_executed_actions_within_window(tmp_path):
    _write_logs(tmp_path, [
        {"timestamp": "2026-07-01T09:35:00-05:00", "trades_executed": [
            {"action": "BUY", "ticker": "nvda"},
            {"action": "HOLD", "ticker": "MSFT"},        # excluded (not executed action)
        ]},
        {"timestamp": "2025-01-01T09:35:00-06:00", "trades_executed": [
            {"action": "SELL", "ticker": "OLD"},          # excluded (older than 183d)
        ]},
    ], [
        {"timestamp": "2026-07-08T15:00:00-05:00", "trades_executed": [
            {"action": "DOUBLE_DOWN", "ticker": "META"},
        ]},
    ])
    result = load_executed_decisions(now=date(2026, 7, 13))
    assert [(d.date, d.ticker, d.action) for d in result] == [
        ("2026-07-08", "META", "DOUBLE_DOWN"),   # newest first
        ("2026-07-01", "NVDA", "BUY"),
    ]


def test_caps_at_max_decisions(tmp_path):
    entries = [{"timestamp": f"2026-07-{d:02d}T09:35:00-05:00",
                "trades_executed": [{"action": "BUY", "ticker": f"T{d}"}]} for d in range(1, 13)]
    _write_logs(tmp_path, entries, [])
    result = load_executed_decisions(now=date(2026, 7, 13))
    assert len(result) == min(12, MAX_DECISIONS)
    assert result[0].date == "2026-07-12"  # newest first
