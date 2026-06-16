"""
claude_callouts.py — Public-safe summary of Claude Portfolio Manager trade history.

Reads the rebalance log and returns sanitized callouts: ticker, action, date, win/loss.
No dollar amounts, quantities, or portfolio weights are exposed.
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_LOG_PATH = os.getenv("CLAUDE_REBALANCE_LOG_PATH", "/data/claude_rebalance_log.json")

_ACTION_EMOJI = {
    "BUY":         "🟢",
    "DOUBLE_DOWN": "🔥",
    "SELL":        "🔴",
    "TRIM":        "✂️",
}


def get_claude_callouts() -> list:
    """Return all Claude Manager trade callouts, newest first, max 100."""
    if not os.path.exists(_LOG_PATH):
        return []
    try:
        with open(_LOG_PATH) as f:
            entries = json.load(f)
    except Exception as exc:
        log.warning("Failed to read rebalance log: %s", exc)
        return []

    callouts = []
    for entry in entries:
        if entry.get("status") != "completed":
            continue
        ts = entry.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts).astimezone(timezone.utc)
            date_str = dt.strftime("%b %-d, %Y")
        except Exception:
            date_str = ts[:10] if ts else "—"

        for trade in entry.get("trades_executed", []):
            action = trade.get("action", "").upper()
            ticker = trade.get("ticker", "").upper()
            if not action or not ticker:
                continue

            callout = {
                "date":   date_str,
                "action": action,
                "ticker": ticker,
                "emoji":  _ACTION_EMOJI.get(action, "📌"),
                "won":    None,
            }
            if action in ("SELL", "TRIM") and trade.get("dollar_pnl") is not None:
                callout["won"] = trade["dollar_pnl"] >= 0

            callouts.append(callout)

    callouts.reverse()
    return callouts[:100]
