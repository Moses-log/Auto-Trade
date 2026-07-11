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


def test_stale_inspection_note_does_not_override_newer_rebalance_thesis():
    """A rebalance that ran after an old Inspection note should win — the
    note predates the rebalance's own updated thesis and must not overlay it."""
    from app.claude_inspection import _build_prior_thesis_map
    divider = "══════════════════════════════"
    rebalance_records = [{
        "timestamp": "2026-07-13T09:35:00",
        "analysis_body": f"{divider}\n## NVDA — NVIDIA Corp\nFresh monthly thesis after rebalance.\n",
    }]
    inspection_records = [{
        "timestamp": "2026-07-06T09:35:00",  # before the rebalance ran
        "notes": {"NVDA": "Stale note from before this rebalance ran."},
    }]
    result = _build_prior_thesis_map(rebalance_records, inspection_records)
    assert "Fresh monthly thesis" in result["NVDA"]
    assert "Stale note" not in result["NVDA"]


def test_falls_back_to_older_rebalance_when_latest_has_no_analysis_body():
    """If the most recent rebalance failed before producing research, fall
    back to the last one that actually did, instead of leaving an empty thesis."""
    from app.claude_inspection import _build_prior_thesis_map
    divider = "══════════════════════════════"
    rebalance_records = [
        {
            "timestamp": "2026-06-01T09:35:00",
            "analysis_body": f"{divider}\n## NVDA — NVIDIA Corp\nJune thesis.\n",
        },
        {
            "timestamp": "2026-07-01T09:35:00",
            "status": "failed_fetch",
            "analysis_body": "",
        },
    ]
    result = _build_prior_thesis_map(rebalance_records, [])
    assert "June thesis" in result["NVDA"]
