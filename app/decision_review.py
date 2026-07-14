"""decision_review.py — Score past executed trades vs SPY and build a scorecard.

Derived live from the rebalance + inspection decision logs and yfinance prices.
No persisted state. Imports of app.claude_manager are lazy (inside functions)
to avoid an import cycle, since claude_manager imports this module.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import yfinance as yf

log = logging.getLogger(__name__)

WINDOW_DAYS = 183
MAX_DECISIONS = 20
NEUTRAL_BAND = 0.015

_EXECUTED_ACTIONS = {"BUY", "SELL", "TRIM", "DOUBLE_DOWN"}


@dataclass
class Decision:
    date: str      # "YYYY-MM-DD"
    ticker: str
    action: str


@dataclass
class DecisionOutcome:
    decision: Decision
    stock_return: float
    spy_return: float
    rel: float
    verdict: str   # "good" | "bad" | "neutral"


@dataclass
class Scorecard:
    outcomes: list = field(default_factory=list)
    skipped: int = 0
    by_action: dict = field(default_factory=dict)   # action -> (good, bad, neutral)


def _read_log(path: str) -> list:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_executed_decisions(now: date | None = None) -> list:
    """Executed BUY/SELL/TRIM/DOUBLE_DOWN from both logs within WINDOW_DAYS,
    newest first, capped at MAX_DECISIONS."""
    now = now or date.today()
    cutoff = now - timedelta(days=WINDOW_DAYS)
    paths = [
        os.getenv("CLAUDE_REBALANCE_LOG_PATH", "/data/claude_rebalance_log.json"),
        os.getenv("CLAUDE_INSPECTION_LOG_PATH", "/data/claude_inspection_log.json"),
    ]
    decisions: list = []
    for path in paths:
        for entry in _read_log(path):
            try:
                d = datetime.fromisoformat(entry.get("timestamp", "")).date()
            except Exception:
                continue
            if d < cutoff:
                continue
            for trade in entry.get("trades_executed", []):
                action = trade.get("action")
                ticker = trade.get("ticker")
                if action in _EXECUTED_ACTIONS and ticker:
                    decisions.append(Decision(date=d.isoformat(), ticker=ticker.upper(), action=action))
    decisions.sort(key=lambda x: x.date, reverse=True)
    return decisions[:MAX_DECISIONS]
