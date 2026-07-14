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


from app.claude_manager import annotate_and_collect_gaps


def test_annotate_sets_field_and_returns_map():
    enriched = [
        {"ticker": "MSFT", "rsi": 55.0, "sma200_pct": 0.1, "perf_qtd": 0.05,
         "forward_pe": 30.0, "revenue_growth_yoy": 0.15},
        {"ticker": "NVDA", "rsi": None, "sma200_pct": 0.2, "perf_qtd": 0.1,
         "forward_pe": 40.0, "revenue_growth_yoy": 0.5},
    ]
    result = annotate_and_collect_gaps(enriched)
    assert result == {"NVDA": ["rsi"]}
    assert "_data_gaps" not in enriched[0]           # complete holding untouched
    assert enriched[1]["_data_gaps"] == ["rsi"]      # gap holding annotated


def test_annotate_empty_when_all_complete():
    enriched = [{"ticker": "MSFT", "rsi": 55.0, "sma200_pct": 0.1, "perf_qtd": 0.05,
                 "forward_pe": 30.0, "revenue_growth_yoy": 0.15}]
    assert annotate_and_collect_gaps(enriched) == {}
    assert "_data_gaps" not in enriched[0]


from app.claude_manager import format_data_gap_field


def test_format_returns_none_when_empty():
    assert format_data_gap_field({}) is None


def test_format_builds_sorted_field():
    field = format_data_gap_field({"NVDA": ["rsi"], "META": ["forward_pe", "revenue_growth_yoy"]})
    assert field["name"] == "⚠️ Data gaps"
    # tickers sorted deterministically; META before NVDA
    assert field["value"] == "META (forward_pe, revenue_growth_yoy); NVDA (rsi)"
    assert field["inline"] is False
