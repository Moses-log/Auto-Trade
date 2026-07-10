"""
claude_inspection.py — Weekly Kimi Inspection: a lightweight, holdings-only
review that runs on the first trading day of the week (skipped when it
coincides with the monthly rebalance), with authority to SELL, TRIM, or
DOUBLE_DOWN — never BUY. See docs/superpowers/specs/2026-07-09-kimi-inspection-design.md.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

_INSPECTION_LOG_PATH = os.getenv("CLAUDE_INSPECTION_LOG_PATH", "/data/claude_inspection_log.json")


def _append_inspection_log(entry: dict) -> None:
    try:
        try:
            with open(_INSPECTION_LOG_PATH) as f:
                records = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            records = []
        records.append(entry)
        if len(records) > 36:          # cap at ~3 years of weekly logs (52/yr, generous)
            records = records[-36:]
        with open(_INSPECTION_LOG_PATH, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as exc:
        log.warning("Failed to write inspection log: %s", exc)


def _load_recent_inspection_entries(limit: int = 5) -> list[dict]:
    try:
        with open(_INSPECTION_LOG_PATH) as f:
            records = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return records[-limit:]


async def run_weekly_inspection() -> None:
    """Placeholder — replaced with the real implementation in Task 8/9.

    Exists now so app/scheduler.py (Task 4) can import this name at module
    level; @patch("app.scheduler.run_weekly_inspection", ...) requires the
    attribute to already exist (patch's default create=False), which in turn
    requires app.claude_inspection.run_weekly_inspection to exist. Task 8
    replaces this entire function body — not append a second definition.
    """
    raise NotImplementedError("run_weekly_inspection is implemented in Task 8/9")
