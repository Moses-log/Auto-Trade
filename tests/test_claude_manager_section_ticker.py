import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")


def test_section_ticker_extracts_symbol_with_em_dash():
    from app.claude_manager import _section_ticker
    section = "## NVDA — NVIDIA Corp\nCurrent: 8%  →  Target: 10%\n"
    assert _section_ticker(section) == "NVDA"


def test_section_ticker_extracts_symbol_with_en_dash():
    from app.claude_manager import _section_ticker
    section = "## META – Meta Platforms\n"
    assert _section_ticker(section) == "META"


def test_section_ticker_returns_empty_when_no_header():
    from app.claude_manager import _section_ticker
    assert _section_ticker("no header here") == ""


def test_divider_is_module_level_string():
    from app.claude_manager import _DIVIDER
    assert isinstance(_DIVIDER, str) and len(_DIVIDER) > 0
