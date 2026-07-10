import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — see Task 6's note on the
# test_config_rh.py alphabetical-collision regression found in Task 5.


def test_extracts_per_ticker_thesis_from_last_rebalance():
    from app.claude_inspection import _build_prior_thesis_map
    divider = "══════════════════════════════"
    analysis_body = (
        f"{divider}\n## NVDA — NVIDIA Corp\nStrong AI infra moat.\n"
        f"{divider}\n## META — Meta Platforms\nAd business reaccelerating.\n"
    )
    rebalance_records = [{"timestamp": "2026-07-01T09:35:00", "analysis_body": analysis_body}]
    result = _build_prior_thesis_map(rebalance_records, [])
    assert "NVDA" in result and "Strong AI infra moat." in result["NVDA"]
    assert "META" in result and "reaccelerating" in result["META"]


def test_returns_empty_map_when_no_rebalance_history():
    from app.claude_inspection import _build_prior_thesis_map
    assert _build_prior_thesis_map([], []) == {}


def test_inspection_entry_overrides_older_rebalance_thesis():
    from app.claude_inspection import _build_prior_thesis_map
    divider = "══════════════════════════════"
    rebalance_records = [{
        "timestamp": "2026-07-01T09:35:00",
        "analysis_body": f"{divider}\n## NVDA — NVIDIA Corp\nOriginal monthly thesis.\n",
    }]
    inspection_records = [{
        "timestamp": "2026-07-13T09:35:00",
        "notes": {"NVDA": "Updated after Inspection: earnings beat, raising conviction."},
    }]
    result = _build_prior_thesis_map(rebalance_records, inspection_records)
    assert "raising conviction" in result["NVDA"]
