import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — this file doesn't need Robinhood
# credentials (it only tests prompt/parser logic), and Task 5 hit a real
# regression where a "test_cla..." file setting these via setdefault() sorted
# alphabetically before tests/test_config_rh.py and polluted its
# settings.rh_username/rh_password-is-None assertions for the whole pytest run.


def test_parse_accepts_hold_sell_trim_double_down():
    from app.claude_inspection import _parse_inspection_trade_block
    text = '''Some analysis text.
```json
{
  "no_changes": false,
  "trades": [
    {"action": "HOLD", "ticker": "MSFT"},
    {"action": "SELL", "ticker": "NOW"},
    {"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 8},
    {"action": "DOUBLE_DOWN", "ticker": "META", "target_weight_pct": 22}
  ]
}
```'''
    result = _parse_inspection_trade_block(text)
    assert result is not None
    actions = {t["action"] for t in result["trades"]}
    assert actions == {"HOLD", "SELL", "TRIM", "DOUBLE_DOWN"}


def test_parse_rejects_buy_action():
    from app.claude_inspection import _parse_inspection_trade_block
    text = '''```json
{
  "no_changes": false,
  "trades": [
    {"action": "BUY", "ticker": "FICO", "target_weight_pct": 10}
  ]
}
```'''
    result = _parse_inspection_trade_block(text)
    assert result is None


def test_parse_rejects_mixed_block_containing_any_buy():
    """A block with one legitimate TRIM and one disallowed BUY is rejected wholesale
    rather than silently dropping the BUY and executing the rest — a model that
    proposes a BUY during Inspection indicates a prompt/constraint failure worth
    surfacing loudly, not papering over."""
    from app.claude_inspection import _parse_inspection_trade_block
    text = '''```json
{
  "no_changes": false,
  "trades": [
    {"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 8},
    {"action": "BUY", "ticker": "FICO", "target_weight_pct": 10}
  ]
}
```'''
    result = _parse_inspection_trade_block(text)
    assert result is None


def test_parse_returns_none_when_no_json_block():
    from app.claude_inspection import _parse_inspection_trade_block
    assert _parse_inspection_trade_block("no json here") is None


def test_search_tool_has_reduced_budget():
    from app.claude_inspection import _INSPECTION_WEB_SEARCH_TOOL
    assert _INSPECTION_WEB_SEARCH_TOOL["max_uses"] <= 15
