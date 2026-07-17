import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — see the alphabetical-collision
# regression note in tests/test_config_rh.py.


def test_realized_proceeds_empty_is_zero():
    from app.claude_manager import _realized_sell_proceeds
    assert _realized_sell_proceeds([]) == 0.0


def test_realized_proceeds_sums_executed_sell_and_trim():
    from app.claude_manager import _realized_sell_proceeds
    executed = [
        {"action": "SELL", "ticker": "ARM", "qty": 10.0, "fill_price": 200.0},
        {"action": "TRIM", "ticker": "NVDA", "qty": 5.0, "fill_price": 100.0},
    ]
    assert _realized_sell_proceeds(executed) == 2500.0


def test_realized_proceeds_ignores_buys_and_holds():
    from app.claude_manager import _realized_sell_proceeds
    executed = [
        {"action": "SELL", "ticker": "ARM", "qty": 10.0, "fill_price": 200.0},
        {"action": "DOUBLE_DOWN", "ticker": "NVDA", "qty": 4.0, "fill_price": 450.0},
        {"action": "BUY", "ticker": "MSFT", "qty": 2.0, "fill_price": 300.0},
    ]
    assert _realized_sell_proceeds(executed) == 2000.0   # only the SELL counts


def test_realized_proceeds_excludes_skipped_sells_by_construction():
    """A proposed SELL that skipped/failed is never recorded in
    trades_executed, so it contributes no phantom cash to the buy budget."""
    from app.claude_manager import _realized_sell_proceeds
    # SELL ARM was proposed but skipped — only the executed DOUBLE_DOWN is here.
    executed = [{"action": "DOUBLE_DOWN", "ticker": "NVDA", "qty": 1.0, "fill_price": 450.0}]
    assert _realized_sell_proceeds(executed) == 0.0


def test_realized_proceeds_counts_queued_sell_at_estimated_fill():
    """Queued after-hours sells store their estimated fill as fill_price, so
    they still count toward the budget (proceeds are expected to settle)."""
    from app.claude_manager import _realized_sell_proceeds
    executed = [{"action": "SELL", "ticker": "ARM", "qty": 10.0, "fill_price": 199.5, "queued": True}]
    assert _realized_sell_proceeds(executed) == 1995.0


def test_realized_proceeds_handles_missing_fill_price():
    from app.claude_manager import _realized_sell_proceeds
    executed = [{"action": "SELL", "ticker": "ARM", "qty": 10.0, "fill_price": None}]
    assert _realized_sell_proceeds(executed) == 0.0
