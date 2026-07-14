import os
os.environ.setdefault("ALPACA_API_KEY", "t")
os.environ.setdefault("ALPACA_SECRET_KEY", "t")
os.environ.setdefault("WEBHOOK_SECRET", "s")

from app.risk_guardrails import clamp_position_weights, ClampEvent, MAX_POSITION_PCT


def test_clamps_oversized_buy_and_double_down_and_trim():
    trades = [
        {"action": "BUY", "ticker": "nvda", "target_weight_pct": 32},
        {"action": "DOUBLE_DOWN", "ticker": "META", "target_weight_pct": 40},
        {"action": "TRIM", "ticker": "AMD", "target_weight_pct": 30},
    ]
    events = clamp_position_weights(trades)
    assert all(t["target_weight_pct"] == MAX_POSITION_PCT for t in trades)
    assert {e.ticker for e in events} == {"NVDA", "META", "AMD"}
    assert events[0].original_pct == 32 and events[0].clamped_pct == 25.0


def test_leaves_within_cap_and_sell_hold_untouched():
    trades = [
        {"action": "BUY", "ticker": "MSFT", "target_weight_pct": 20},
        {"action": "SELL", "ticker": "NOW"},
        {"action": "HOLD", "ticker": "GOOG", "target_weight_pct": 30},   # HOLD not clamped
        {"action": "TRIM", "ticker": "AAPL"},                            # missing pct: no error
    ]
    events = clamp_position_weights(trades)
    assert events == []
    assert trades[0]["target_weight_pct"] == 20
    assert trades[2]["target_weight_pct"] == 30
