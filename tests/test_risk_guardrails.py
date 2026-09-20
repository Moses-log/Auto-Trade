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


# append to tests/test_risk_guardrails.py
from app.risk_guardrails import resolve_sectors, format_guardrail_embed, ClampEvent


def test_resolve_sectors_uses_enriched_then_fetch():
    enriched = [{"ticker": "NVDA", "sector": "Technology"}]
    trades = [
        {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25},  # known from enriched
        {"action": "BUY", "ticker": "AVGO", "target_weight_pct": 20},          # needs fetch -> ok
        {"action": "BUY", "ticker": "ZZZ", "target_weight_pct": 10},           # fetch -> None
    ]
    calls = {"AVGO": "Technology", "ZZZ": None}
    sector_map, unknown = resolve_sectors(enriched, trades, lambda t: calls.get(t))
    assert sector_map["NVDA"] == "Technology"
    assert sector_map["AVGO"] == "Technology"
    assert unknown == ["ZZZ"]


def test_embed_none_when_nothing_fired():
    assert format_guardrail_embed([], [], []) is None


def test_embed_has_clamp_and_sector_fields():
    embed = format_guardrail_embed(
        [ClampEvent("NVDA", 32.0, 25.0)],
        ["Technology 68% (> 50% cap)"],
        ["ZZZ"],
    )
    assert embed["title"] == "⚠️ RISK GUARDRAIL"
    joined = " ".join(f["value"] for f in embed["fields"])
    assert "NVDA" in joined and "Technology 68%" in joined and "ZZZ" in joined


def test_trim_skip_reason_allows_sub_share_position_over_one_dollar():
    from app.risk_guardrails import trim_skip_reason
    # 0.5 share @ $300 = $150 position, selling $112.50 -- fine on Robinhood.
    assert trim_skip_reason(current_value=150.0, sell_value=112.5) is None


def test_trim_skip_reason_blocks_position_at_or_under_one_dollar():
    from app.risk_guardrails import trim_skip_reason
    assert "under $1" in trim_skip_reason(current_value=0.60, sell_value=0.30)
    assert trim_skip_reason(current_value=1.0, sell_value=0.5) is not None


def test_trim_skip_reason_blocks_sell_under_one_dollar():
    from app.risk_guardrails import trim_skip_reason
    assert "under $1" in trim_skip_reason(current_value=50.0, sell_value=0.40)


def _pos(sym, qty, px):
    return {"symbol": sym, "qty": qty, "current_price": px}


def test_enforce_position_cap_turns_hold_into_trim_to_cap():
    from app.risk_guardrails import enforce_position_cap
    trades = [{"action": "HOLD", "ticker": "NVDA"}, {"action": "HOLD", "ticker": "MSFT"}]
    positions = [_pos("NVDA", 10, 350.0), _pos("MSFT", 10, 100.0)]   # NVDA 35%, MSFT 10%
    forced = enforce_position_cap(trades, positions, 10000.0)
    assert [f["ticker"] for f in forced] == ["NVDA"]
    nvda = trades[0]
    assert nvda["action"] == "TRIM" and nvda["target_weight_pct"] == 25.0
    assert "cap" in nvda["reasoning"].lower()
    assert trades[1] == {"action": "HOLD", "ticker": "MSFT"}


def test_enforce_position_cap_adds_trade_when_ticker_missing():
    from app.risk_guardrails import enforce_position_cap
    trades = []
    forced = enforce_position_cap(trades, [_pos("NVDA", 10, 350.0)], 10000.0)
    assert len(forced) == 1 and trades[0]["action"] == "TRIM"


def test_enforce_position_cap_overrides_double_down_on_overweight():
    from app.risk_guardrails import enforce_position_cap
    trades = [{"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 40}]
    enforce_position_cap(trades, [_pos("NVDA", 10, 350.0)], 10000.0)
    assert trades[0]["action"] == "TRIM" and trades[0]["target_weight_pct"] == 25.0


def test_enforce_position_cap_leaves_sell_trim_and_under_cap_alone():
    from app.risk_guardrails import enforce_position_cap
    trades = [
        {"action": "SELL", "ticker": "NVDA"},
        {"action": "TRIM", "ticker": "AMD", "target_weight_pct": 10, "reasoning": "x"},
        {"action": "HOLD", "ticker": "MSFT"},
    ]
    positions = [_pos("NVDA", 10, 350.0), _pos("AMD", 10, 350.0), _pos("MSFT", 10, 100.0)]
    forced = enforce_position_cap(trades, positions, 10000.0)
    assert [f["ticker"] for f in forced] == []      # SELL/TRIM handled; MSFT is 10%
    assert trades[0]["action"] == "SELL"
    assert trades[1]["target_weight_pct"] == 10


def test_enforce_position_cap_ignores_spy_and_zero_portfolio():
    from app.risk_guardrails import enforce_position_cap
    trades = []
    assert enforce_position_cap(trades, [_pos("SPY", 100, 500.0)], 50000.0) == []
    assert enforce_position_cap(trades, [_pos("NVDA", 1, 1.0)], 0.0) == []
    assert trades == []
