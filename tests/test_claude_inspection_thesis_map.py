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


def test_living_thesis_layers_inspection_update_on_anchor_not_replaces():
    """Living Thesis: a newer inspection note is appended UNDER the rebalance
    anchor thesis, not substituted for it. The anchor research survives."""
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
    nvda = result["NVDA"]
    assert "Original monthly thesis." in nvda          # anchor preserved
    assert "raising conviction" in nvda                # delta appended
    assert "Updates since last rebalance:" in nvda     # under a labeled section
    assert "2026-07-13" in nvda                        # dated


def test_living_thesis_caps_deltas_at_four_most_recent():
    """Only the last 4 inspection updates per ticker are kept (≈ one rebalance
    cycle of weekly inspections); anything older drops."""
    from app.claude_inspection import _build_prior_thesis_map
    divider = "══════════════════════════════"
    rebalance_records = [{
        "timestamp": "2026-07-01T09:35:00",
        "analysis_body": f"{divider}\n## NVDA — NVIDIA Corp\nAnchor thesis.\n",
    }]
    inspection_records = [
        {"timestamp": "2026-07-04T09:35:00", "notes": {"NVDA": "note-one-oldest"}},
        {"timestamp": "2026-07-11T09:35:00", "notes": {"NVDA": "note-two"}},
        {"timestamp": "2026-07-18T09:35:00", "notes": {"NVDA": "note-three"}},
        {"timestamp": "2026-07-25T09:35:00", "notes": {"NVDA": "note-four"}},
        {"timestamp": "2026-07-31T09:35:00", "notes": {"NVDA": "note-five-newest"}},
    ]
    result = _build_prior_thesis_map(rebalance_records, inspection_records)
    nvda = result["NVDA"]
    assert "note-one-oldest" not in nvda               # oldest dropped by the cap
    assert "note-two" in nvda
    assert "note-three" in nvda
    assert "note-four" in nvda
    assert "note-five-newest" in nvda


def test_living_thesis_orders_deltas_oldest_to_newest():
    """Kept updates read chronologically so thesis evolution is legible."""
    from app.claude_inspection import _build_prior_thesis_map
    divider = "══════════════════════════════"
    rebalance_records = [{
        "timestamp": "2026-07-01T09:35:00",
        "analysis_body": f"{divider}\n## NVDA — NVIDIA Corp\nAnchor thesis.\n",
    }]
    inspection_records = [
        {"timestamp": "2026-07-11T09:35:00", "notes": {"NVDA": "earlier-update"}},
        {"timestamp": "2026-07-18T09:35:00", "notes": {"NVDA": "later-update"}},
    ]
    result = _build_prior_thesis_map(rebalance_records, inspection_records)
    nvda = result["NVDA"]
    assert nvda.index("earlier-update") < nvda.index("later-update")


def test_living_thesis_shows_notes_when_no_anchor_on_record():
    """A ticker acted on by inspection but absent from the last rebalance body
    still surfaces its notes rather than vanishing."""
    from app.claude_inspection import _build_prior_thesis_map
    rebalance_records = []
    inspection_records = [{
        "timestamp": "2026-07-13T09:35:00",
        "notes": {"ARM": "Trimmed on valuation after a sharp run."},
    }]
    result = _build_prior_thesis_map(rebalance_records, inspection_records)
    assert "ARM" in result
    assert "Trimmed on valuation" in result["ARM"]


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
