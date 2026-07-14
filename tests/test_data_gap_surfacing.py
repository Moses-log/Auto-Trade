import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from app.claude_manager import compute_data_gaps, _CRITICAL_DATA_FIELDS


def _full_holding():
    return {
        "ticker": "MSFT",
        "rsi": 55.0, "sma200_pct": 0.1, "perf_qtd": 0.05,
        "forward_pe": 30.0, "revenue_growth_yoy": 0.15,
        "short_pct_float": None,  # minor field, must NOT be flagged
    }


def test_full_holding_has_no_gaps():
    assert compute_data_gaps(_full_holding()) == []


def test_missing_technical_is_flagged():
    h = _full_holding(); h["rsi"] = None
    assert compute_data_gaps(h) == ["rsi"]


def test_missing_fundamental_is_flagged():
    h = _full_holding(); del h["forward_pe"]
    assert compute_data_gaps(h) == ["forward_pe"]


def test_minor_field_none_not_flagged():
    assert compute_data_gaps(_full_holding()) == []  # short_pct_float None ignored


def test_total_failure_flags_all_sorted():
    assert compute_data_gaps({"ticker": "X"}) == sorted(_CRITICAL_DATA_FIELDS)
