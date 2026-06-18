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


def _load_deposit_events() -> list[tuple[str, float]]:
    """Load deposit events from investors.json for TWR computation."""
    try:
        from app.investors import get_deposit_events
        return get_deposit_events()
    except Exception as exc:
        log.warning("Could not load deposit events for TWR: %s", exc)
        return []

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


def get_claude_performance() -> dict:
    """Return portfolio vs SPY performance using Time-Weighted Return (TWR).

    TWR chains sub-period returns between rebalances, subtracting any external
    cash deposits so the chart reflects actual trading performance — not account
    growth from new capital injections.
    """
    empty = {"data_points": [], "portfolio_pct": 0.0, "spy_pct": 0.0, "alpha": 0.0, "inception": None}
    if not os.path.exists(_LOG_PATH):
        return empty
    try:
        with open(_LOG_PATH) as f:
            entries = json.load(f)
    except Exception as exc:
        log.warning("Failed to read rebalance log for performance: %s", exc)
        return empty

    completed = [
        e for e in entries
        if e.get("status") == "completed"
        and e.get("portfolio_value")
        and e.get("spy_price_at_rebalance")
    ]
    if not completed:
        return empty

    deposit_events = _load_deposit_events()
    first_spy = completed[0]["spy_price_at_rebalance"]

    data_points = []
    cumulative_twr = 1.0
    prev_value: float | None = None
    prev_date = ""

    for i, entry in enumerate(completed):
        ts = entry.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts).astimezone(timezone.utc)
            label = dt.strftime("%b %Y")
            curr_date = dt.strftime("%Y-%m-%d")
        except Exception:
            label = ts[:7] if ts else "—"
            curr_date = ts[:10] if ts else ""

        spy_pct = round((entry["spy_price_at_rebalance"] / first_spy - 1) * 100, 2)

        if i == 0:
            data_points.append({"label": label, "portfolio_pct": 0.0, "spy_pct": 0.0})
            prev_value = entry["portfolio_value"]
            prev_date = curr_date
            continue

        curr_value = entry["portfolio_value"]
        deposits_in_period = sum(
            amt for dt, amt in deposit_events
            if prev_date < dt <= curr_date
        )

        if prev_value and prev_value > 0:
            sub_return = (curr_value - deposits_in_period) / prev_value - 1
        else:
            sub_return = 0.0

        cumulative_twr *= (1 + sub_return)
        portfolio_pct = round((cumulative_twr - 1) * 100, 2)

        data_points.append({
            "label": label,
            "portfolio_pct": portfolio_pct,
            "spy_pct": spy_pct,
        })
        prev_value = curr_value
        prev_date = curr_date

    final_port = data_points[-1]["portfolio_pct"]
    final_spy = data_points[-1]["spy_pct"]
    inception = None
    try:
        dt = datetime.fromisoformat(completed[0]["timestamp"]).astimezone(timezone.utc)
        inception = dt.strftime("%B %Y")
    except Exception:
        pass

    return {
        "data_points": data_points,
        "portfolio_pct": round(final_port, 2),
        "spy_pct": round(final_spy, 2),
        "alpha": round(final_port - final_spy, 2),
        "inception": inception,
    }
