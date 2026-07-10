import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — rh_client is fully mocked in every
# test below, and settings.rh_username/rh_password default to None (Optional),
# so no real value is ever needed. See Task 6's note on the alphabetical-
# collision regression with tests/test_config_rh.py found in Task 5.

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_position(symbol="NVDA", qty=10.0, avg_entry=400.0, current_price=450.0):
    return {
        "symbol": symbol, "qty": qty, "avg_entry_price": avg_entry,
        "current_price": current_price, "unrealized_pl": (current_price - avg_entry) * qty,
        "unrealized_plpc": (current_price / avg_entry - 1) * 100,
    }


@pytest.mark.asyncio
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection.rh_client")
async def test_skips_when_rh_session_unavailable(mock_rh, mock_notify):
    mock_rh.available = False
    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()
    mock_notify.assert_awaited_once()
    assert "offline" in mock_notify.call_args[0][0]["title"].lower() or \
           "offline" in mock_notify.call_args[0][0].get("description", "").lower()


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_no_changes_posts_notification_and_logs(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse, mock_notify, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[_mock_position()])
    mock_call.return_value = "analysis text ```json\n{}\n```"
    mock_parse.return_value = {"no_changes": True, "trades": []}

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_log.assert_called_once()
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "no_changes"
    assert any("no material changes" in c.kwargs.get("description", "").lower()
               or "no material changes" in str(c.args).lower()
               for c in mock_notify.call_args_list)


@pytest.mark.asyncio
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._call_claude_inspection_sync", side_effect=RuntimeError("API down"))
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_claude_api_failure_notifies_and_does_not_raise(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_notify,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[_mock_position()])

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()  # must not raise

    assert mock_notify.await_count >= 1
