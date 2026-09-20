import os
os.environ.setdefault("ALPACA_API_KEY", "t")
os.environ.setdefault("ALPACA_SECRET_KEY", "t")
os.environ.setdefault("WEBHOOK_SECRET", "s")
os.environ.setdefault("ANTHROPIC_API_KEY", "t")

from app.claude_inspection import _INSPECTION_SYSTEM_PROMPT as P


def test_prompt_no_longer_defaults_to_hold():
    assert "DEFAULT TO HOLD" not in P
    assert "manufacture a reason" not in P


def test_prompt_lists_broader_action_triggers():
    low = P.lower()
    assert "overweight" in low or "over ~20%" in low     # concentration trigger
    assert "valuation" in low
    assert "thesis drift" in low or "weakening" in low
    assert "momentum" in low


def test_prompt_keeps_hard_guardrails():
    assert "never propose BUY" in P
    assert "maximum position size 25%" in P
    assert "SPY is permanently excluded" in P
