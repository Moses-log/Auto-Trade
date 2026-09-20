import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection.get_record", return_value=(5, 2))
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.claude_inspection.trim_position", return_value=(2.857143, 10.0, 5.0))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "X"})
@patch("app.claude_inspection.rh_client")
async def test_overweight_holding_is_force_trimmed_to_cap_even_when_model_says_hold(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_trim_position, mock_record_rh_trade, mock_get_record, mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[
        {"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 300.0,
         "current_price": 350.0, "unrealized_pl": 500.0, "unrealized_plpc": 16.7},   # $3,500
        {"symbol": "MSFT", "qty": 10.0, "avg_entry_price": 90.0,
         "current_price": 100.0, "unrealized_pl": 100.0, "unrealized_plpc": 11.1},   # $1,000
    ])
    mock_rh.get_buying_power_async = AsyncMock(return_value=5500.0)   # portfolio = $10,000
    mock_rh.sell_shares_async = AsyncMock(
        return_value={"status": "ok", "qty": 2.857143, "fill_price": 350.0}
    )
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": True,
        "trades": [{"action": "HOLD", "ticker": "NVDA"}, {"action": "HOLD", "ticker": "MSFT"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    # NVDA is 35% of $10,000; 25% target = $2,500; sell (3500 - 2500) / 350 = 2.857143 shares.
    mock_rh.sell_shares_async.assert_awaited_once_with("NVDA", 2.857143)
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "completed"
    assert logged_entry["forced_trims"][0]["ticker"] == "NVDA"
    assert logged_entry["trades_executed"][0]["ticker"] == "NVDA"
    assert "cap" in logged_entry["trades_executed"][0]["reasoning"].lower()
