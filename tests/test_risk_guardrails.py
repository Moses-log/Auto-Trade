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


# append to tests/test_risk_guardrails.py
from app.risk_guardrails import compute_sector_exposure, sector_warnings


def test_sector_exposure_post_trade_weights():
    positions = [
        {"symbol": "NVDA", "qty": 1, "current_price": 100.0},   # current 10%
        {"symbol": "AMD",  "qty": 1, "current_price": 200.0},   # current 20%
        {"symbol": "KO",   "qty": 1, "current_price": 100.0},   # current 10%
    ]
    trades = [
        {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25},
        {"action": "SELL", "ticker": "AMD"},                    # -> 0
        {"action": "BUY", "ticker": "AVGO", "target_weight_pct": 30},  # new, tech
        # KO has no trade -> stays at current 10%
    ]
    sectors = {"NVDA": "Technology", "AMD": "Technology", "AVGO": "Technology", "KO": "Consumer Defensive"}
    exposure = compute_sector_exposure(positions, trades, 1000.0, sectors.get)
    assert round(exposure["Technology"], 1) == 55.0        # 25 (NVDA) + 0 (AMD sold) + 30 (AVGO)
    assert round(exposure["Consumer Defensive"], 1) == 10.0


def test_unknown_sector_bucketed():
    positions = [{"symbol": "XYZ", "qty": 1, "current_price": 600.0}]
    exposure = compute_sector_exposure(positions, [], 1000.0, lambda t: None)
    assert exposure == {"Unknown": 60.0}


def test_sector_warnings_only_over_cap_excluding_unknown():
    assert sector_warnings({"Technology": 68.0, "Energy": 40.0, "Unknown": 90.0}) == \
        ["Technology 68% (> 50% cap)"]
    assert sector_warnings({"Technology": 50.0}) == []   # not strictly over
