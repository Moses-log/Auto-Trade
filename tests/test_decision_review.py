# tests/test_decision_review.py
import json
import os
from datetime import date

from app.decision_review import Decision, load_executed_decisions, MAX_DECISIONS


def _write_logs(tmp_path, rebalance_entries, inspection_entries):
    reb = tmp_path / "reb.json"; insp = tmp_path / "insp.json"
    reb.write_text(json.dumps(rebalance_entries)); insp.write_text(json.dumps(inspection_entries))
    os.environ["CLAUDE_REBALANCE_LOG_PATH"] = str(reb)
    os.environ["CLAUDE_INSPECTION_LOG_PATH"] = str(insp)


def test_extracts_only_executed_actions_within_window(tmp_path):
    _write_logs(tmp_path, [
        {"timestamp": "2026-07-01T09:35:00-05:00", "trades_executed": [
            {"action": "BUY", "ticker": "nvda"},
            {"action": "HOLD", "ticker": "MSFT"},        # excluded (not executed action)
        ]},
        {"timestamp": "2025-01-01T09:35:00-06:00", "trades_executed": [
            {"action": "SELL", "ticker": "OLD"},          # excluded (older than 183d)
        ]},
    ], [
        {"timestamp": "2026-07-08T15:00:00-05:00", "trades_executed": [
            {"action": "DOUBLE_DOWN", "ticker": "META"},
        ]},
    ])
    result = load_executed_decisions(now=date(2026, 7, 13))
    assert [(d.date, d.ticker, d.action) for d in result] == [
        ("2026-07-08", "META", "DOUBLE_DOWN"),   # newest first
        ("2026-07-01", "NVDA", "BUY"),
    ]


def test_caps_at_max_decisions(tmp_path):
    entries = [{"timestamp": f"2026-07-{d:02d}T09:35:00-05:00",
                "trades_executed": [{"action": "BUY", "ticker": f"T{d}"}]} for d in range(1, 13)]
    _write_logs(tmp_path, entries, [])
    result = load_executed_decisions(now=date(2026, 7, 13))
    assert len(result) == min(12, MAX_DECISIONS)
    assert result[0].date == "2026-07-12"  # newest first


# append to tests/test_decision_review.py
from app.decision_review import score_decision, build_scorecard, DecisionOutcome


def _price_fn_factory(table):
    # table: {ticker: (start_close, latest_close) or None}
    def fn(ticker, start_date):
        return table.get(ticker)
    return fn


def test_sell_that_dodged_a_drop_is_good():
    d = Decision("2026-06-01", "XYZ", "SELL")
    # stock fell 10%, SPY rose 2% -> rel = -0.12 -> good sell
    fn = _price_fn_factory({"XYZ": (100.0, 90.0), "SPY": (100.0, 102.0)})
    o = score_decision(d, fn)
    assert o.verdict == "good" and round(o.rel, 2) == -0.12


def test_sell_that_missed_a_rally_is_bad():
    d = Decision("2026-06-01", "XYZ", "SELL")
    fn = _price_fn_factory({"XYZ": (100.0, 115.0), "SPY": (100.0, 103.0)})
    assert score_decision(d, fn).verdict == "bad"


def test_buy_that_beat_spy_is_good():
    d = Decision("2026-06-01", "XYZ", "BUY")
    fn = _price_fn_factory({"XYZ": (100.0, 120.0), "SPY": (100.0, 105.0)})
    assert score_decision(d, fn).verdict == "good"


def test_buy_that_lagged_spy_is_bad():
    d = Decision("2026-06-01", "XYZ", "DOUBLE_DOWN")
    fn = _price_fn_factory({"XYZ": (100.0, 101.0), "SPY": (100.0, 110.0)})
    assert score_decision(d, fn).verdict == "bad"


def test_within_neutral_band_is_neutral():
    fn = _price_fn_factory({"XYZ": (100.0, 101.0), "SPY": (100.0, 100.5)})  # rel = +0.005
    assert score_decision(Decision("2026-06-01", "XYZ", "SELL"), fn).verdict == "neutral"
    assert score_decision(Decision("2026-06-01", "XYZ", "BUY"), fn).verdict == "neutral"


def test_missing_price_returns_none():
    fn = _price_fn_factory({"XYZ": None, "SPY": (100.0, 102.0)})
    assert score_decision(Decision("2026-06-01", "XYZ", "SELL"), fn) is None


def test_build_scorecard_aggregates_and_counts_skipped():
    decisions = [
        Decision("2026-06-01", "AAA", "SELL"),   # good
        Decision("2026-06-02", "BBB", "BUY"),    # bad
        Decision("2026-06-03", "CCC", "SELL"),   # skipped (no data)
    ]
    fn = _price_fn_factory({
        "AAA": (100.0, 80.0), "BBB": (100.0, 90.0), "CCC": None,
        "SPY": (100.0, 100.0),
    })
    sc = build_scorecard(decisions, fn)
    assert sc.skipped == 1
    assert sc.by_action["SELL"] == (1, 0, 0)
    assert sc.by_action["BUY"] == (0, 1, 0)
    assert len(sc.outcomes) == 2


# append to tests/test_decision_review.py
from app.decision_review import format_scorecard_prompt, format_scorecard_embed, Scorecard, DecisionOutcome


def _oc(dt, tk, ac, rel, verdict):
    return DecisionOutcome(Decision(dt, tk, ac), rel, 0.0, rel, verdict)


def test_prompt_empty_when_no_outcomes():
    assert format_scorecard_prompt(Scorecard()) == ""


def test_prompt_text_is_deterministic():
    sc = Scorecard(
        outcomes=[_oc("2026-07-01", "AAA", "SELL", -0.1, "good")] * 4,
        skipped=0,
        by_action={"SELL": (2, 1, 1), "BUY": (1, 0, 0)},
    )
    text = format_scorecard_prompt(sc)
    assert text == (
        "Last 4 executed decisions scored vs SPY since each decision: "
        "BUY: 1/1 good; SELL: 2/4 good, 1 neutral. "
        "Calibrate: repeat what worked, reconsider what didn't."
    )


def test_embed_has_title_and_counts():
    sc = Scorecard(
        outcomes=[_oc("2026-07-01", "AAA", "SELL", -0.1, "good"),
                  _oc("2026-07-02", "BBB", "BUY", 0.05, "good")],
        skipped=1,
        by_action={"SELL": (1, 0, 0), "BUY": (1, 0, 0)},
    )
    embed = format_scorecard_embed(sc)
    assert embed["title"] == "📅 KIMI DECISION REVIEW"
    assert "Scored 2" in embed["description"]
    assert "1 skipped" in embed["description"]
    assert embed["fields"]  # has at least the aggregate field
