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
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection.rh_client")
async def test_skips_when_rh_session_unavailable(mock_rh, mock_notify, mock_log):
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
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
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
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._call_claude_inspection_sync", side_effect=RuntimeError("API down"))
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_claude_api_failure_notifies_and_does_not_raise(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_notify, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[_mock_position()])
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()  # must not raise

    assert mock_notify.await_count >= 1


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.claude_inspection.get_record", return_value=(5, 2))
@patch("app.claude_inspection.close_position", return_value=(10.0, 500.0, 12.5))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NOW"})
@patch("app.claude_inspection.rh_client")
async def test_sell_action_executes_and_notifies_both_channels(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_close_position, mock_get_record, mock_record_rh_trade, mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NOW", "qty": 5.0, "avg_entry_price": 900.0,
                       "current_price": 950.0, "unrealized_pl": 250.0, "unrealized_plpc": 5.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "ok", "qty": 5.0, "fill_price": 960.0, "queued": False}
    )
    mock_call.return_value = "thesis broken ```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "SELL", "ticker": "NOW", "reasoning": "guidance cut"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_rh.close_ticker_async.assert_awaited_once_with("NOW")
    mock_close_position.assert_called_once()
    mock_record_rh_trade.assert_awaited_once_with(True, "NOW", 500.0)
    assert mock_notify_private.await_count >= 1   # Private Server gets full detail
    assert mock_notify_public.await_count >= 1    # KI Server gets the actioned-holding summary
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "completed"
    assert logged_entry["trades_executed"][0]["action"] == "SELL"


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection.get_record", return_value=(5, 2))
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.claude_inspection.trim_position", return_value=(7.0, 350.0, 8.4))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_trim_action_executes_and_notifies_both_channels(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_trim_position, mock_record_rh_trade, mock_get_record, mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 400.0,
                       "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_rh.sell_shares_async = AsyncMock(
        return_value={"status": "ok", "qty": 7.0, "fill_price": 460.0}
    )
    mock_call.return_value = "trim to reduce concentration ```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 30,
                    "reasoning": "trim to reduce concentration"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    # target_weight_pct=30 is clamped to the 25% guardrail ceiling before sizing,
    # so the sell qty reflects trimming down to 25%, not the raw 30% ask.
    mock_rh.sell_shares_async.assert_awaited_once_with("NVDA", 7.5)
    mock_trim_position.assert_called_once()
    mock_record_rh_trade.assert_awaited_once_with(True, "NVDA", 350.0)
    assert mock_notify_private.await_count >= 1   # Private Server gets full detail
    assert mock_notify_public.await_count >= 1    # KI Server gets the actioned-holding summary
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "completed"
    assert logged_entry["trades_executed"][0]["action"] == "TRIM"


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.claude_inspection.open_position")
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_double_down_action_executes_and_notifies_both_channels(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_open_position, mock_record_rh_trade, mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[
            {"symbol": "NVDA", "qty": 2.0, "avg_entry_price": 400.0,
             "current_price": 450.0, "unrealized_pl": 100.0, "unrealized_plpc": 12.5},
            {"symbol": "MSFT", "qty": 20.0, "avg_entry_price": 280.0,
             "current_price": 300.0, "unrealized_pl": 400.0, "unrealized_plpc": 7.1},
        ]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=5000.0)
    mock_rh.buy_dollars_async = AsyncMock(
        return_value={"status": "ok", "qty": 1.0667, "fill_price": 450.0}
    )
    mock_call.return_value = "bull case intact ```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 20,
                    "reasoning": "bull case intact"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    # portfolio_value now includes cash: (2*450 + 20*300) holdings + 5000 buying_power = 11900
    # target 20% -> 2380; current NVDA value 900; delta 1480; budget 5000*0.95=4750 -> invest 1480
    mock_rh.buy_dollars_async.assert_awaited_once_with("NVDA", 1480.0)
    mock_open_position.assert_called_once()
    mock_record_rh_trade.assert_not_awaited()      # DOUBLE_DOWN is buy-side only — no ledger record
    assert mock_notify_private.await_count >= 1    # Private Server gets full detail
    assert mock_notify_public.await_count >= 1     # KI Server gets the actioned-holding summary
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "completed"
    assert logged_entry["trades_executed"][0]["action"] == "DOUBLE_DOWN"


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_double_down_skips_on_insufficient_buying_power(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[
            {"symbol": "NVDA", "qty": 2.0, "avg_entry_price": 400.0,
             "current_price": 450.0, "unrealized_pl": 100.0, "unrealized_plpc": 12.5},
            {"symbol": "MSFT", "qty": 20.0, "avg_entry_price": 280.0,
             "current_price": 300.0, "unrealized_pl": 400.0, "unrealized_plpc": 7.1},
        ]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.50)
    mock_call.return_value = "still bullish, wants to add ```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 90,
                    "reasoning": "still bullish, wants to add"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_rh.buy_dollars_async.assert_not_called()
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "completed"
    skipped = logged_entry["trades_skipped"]
    assert any(t["action"] == "DOUBLE_DOWN" and t["ticker"] == "NVDA" and t.get("reason") for t in skipped)
    assert mock_notify_private.await_count >= 1    # run still posts start/summary notifications


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_double_down_skip_reason_distinguishes_already_at_target_from_low_budget(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    """Plenty of buying power but the position is already at/above its
    target weight — the skip reason must say so, not claim a budget shortfall."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[
            {"symbol": "NVDA", "qty": 20.0, "avg_entry_price": 400.0,
             "current_price": 450.0, "unrealized_pl": 1000.0, "unrealized_plpc": 12.5},
            {"symbol": "MSFT", "qty": 2.0, "avg_entry_price": 280.0,
             "current_price": 300.0, "unrealized_pl": 40.0, "unrealized_plpc": 7.1},
        ]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=50000.0)
    mock_call.return_value = "already large position ```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 10,
                    "reasoning": "already large position"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_rh.buy_dollars_async.assert_not_called()
    logged_entry = mock_log.call_args[0][0]
    skipped_reason = next(
        t["reason"] for t in logged_entry["trades_skipped"]
        if t["action"] == "DOUBLE_DOWN" and t["ticker"] == "NVDA"
    )
    assert "already at target" in skipped_reason
    assert "only $" not in skipped_reason


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_double_down_never_calls_open_position_for_a_buy_action(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    """Belt-and-suspenders: even if a BUY somehow reached this point, the
    execution loop below only dispatches SELL/TRIM/DOUBLE_DOWN branches —
    there is no BUY branch to accidentally execute."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 400.0,
                       "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5}]
    )
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {"no_changes": True, "trades": []}

    from app.claude_inspection import run_weekly_inspection
    import inspect
    source = inspect.getsource(run_weekly_inspection)
    assert '"BUY"' not in source


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "SPY"})
@patch("app.claude_inspection.rh_client")
async def test_spy_is_never_traded_even_if_claude_proposes_it(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "SPY", "qty": 10.0, "avg_entry_price": 400.0,
                       "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "SELL", "ticker": "SPY", "reasoning": "should never happen"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_rh.close_ticker_async.assert_not_called()
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["trades_executed"] == []


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_trim_missing_target_weight_pct_is_skipped_not_defaulted(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 400.0,
                       "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "TRIM", "ticker": "NVDA", "reasoning": "trim a bit"}],  # missing target_weight_pct
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_rh.sell_shares_async.assert_not_called()
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["trades_executed"] == []
    assert any(t["reason"] == "missing target_weight_pct" for t in logged_entry["trades_skipped"])


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_unrecognized_action_is_logged_and_skipped(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 400.0,
                       "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "SHORT", "ticker": "NVDA", "reasoning": "not a real action"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["trades_executed"] == []
    assert any(
        t["action"] == "SHORT" and t["reason"] == "unrecognized action"
        for t in logged_entry["trades_skipped"]
    )


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NOW"})
@patch("app.claude_inspection.rh_client")
async def test_sell_note_skip_is_gray_not_red_failed(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    """close_ticker_async's benign 'note' (e.g. no position to close) must be
    reported as a gray skip, not misclassified as a red FAILED trade."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NOW", "qty": 5.0, "avg_entry_price": 900.0,
                       "current_price": 950.0, "unrealized_pl": 250.0, "unrealized_plpc": 5.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "error", "note": "no open position to close"}
    )
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "SELL", "ticker": "NOW", "reasoning": "thesis broken"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["trades_skipped"][0]["reason"] == "no open position to close"
    private_titles = [c.args[0].get("title", "") for c in mock_notify_private.call_args_list]
    assert any("skipped" in t.lower() for t in private_titles)
    assert not any("FAILED" in t for t in private_titles)


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.scheduler.scheduler")
@patch("app.pending_orders.save_pending_order")
@patch("app.trading.alpaca_client.get_next_trading_day")
@patch("app.claude_inspection.close_position", return_value=(5.0, 500.0, 12.5))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NOW"})
@patch("app.claude_inspection.rh_client")
async def test_queued_sell_defers_record_and_schedules_pending_fill(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_close_position, mock_next_day, mock_save_pending, mock_scheduler,
    mock_record_rh_trade, mock_notify_private, mock_notify_public, mock_log,
):
    """A SELL that queues after-hours must not record P&L immediately (the
    fill price isn't known yet) — it must schedule a pending-fill job instead."""
    from datetime import date
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NOW", "qty": 5.0, "avg_entry_price": 900.0,
                       "current_price": 950.0, "unrealized_pl": 250.0, "unrealized_plpc": 5.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "ok", "qty": 5.0, "price_est": 960.0, "queued": True, "order_id": "abc123"}
    )
    mock_next_day.return_value = date(2026, 7, 13)
    mock_call.return_value = "thesis broken ```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "SELL", "ticker": "NOW", "reasoning": "guidance cut"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    mock_record_rh_trade.assert_not_awaited()
    mock_scheduler.add_job.assert_called_once()
    mock_save_pending.assert_called_once()
    save_kwargs = mock_save_pending.call_args.kwargs
    assert save_kwargs["broker"] == "claude_sell"
    assert save_kwargs["source"] == "manager"
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["trades_executed"][0]["queued"] is True


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_trim_explicit_null_target_weight_pct_is_skipped_not_crashed(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    """An explicit JSON null (key present, value None) must be treated the
    same as a missing key — not passed through into float arithmetic."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 400.0,
                       "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "TRIM", "ticker": "NVDA", "target_weight_pct": None, "reasoning": "trim a bit"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()  # must not raise TypeError

    mock_rh.sell_shares_async.assert_not_called()
    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["status"] == "completed"
    assert any(t["reason"] == "missing target_weight_pct" for t in logged_entry["trades_skipped"])


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection.open_position")
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_skipped_sell_does_not_inflate_double_down_budget(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_open_position, mock_notify_private, mock_notify_public, mock_log,
):
    """A SELL that skips (no position) must NOT contribute phantom proceeds to
    a same-run DOUBLE_DOWN's budget — the buy is sized against real cash only."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[
            {"symbol": "NVDA", "qty": 1.0, "avg_entry_price": 80.0,
             "current_price": 100.0, "unrealized_pl": 20.0, "unrealized_plpc": 25.0},
            {"symbol": "ARM", "qty": 10.0, "avg_entry_price": 150.0,
             "current_price": 200.0, "unrealized_pl": 500.0, "unrealized_plpc": 33.3},
        ]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=500.0)
    # The ARM SELL skips — no proceeds actually materialize.
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "error", "note": "no open position to close"}
    )
    mock_rh.buy_dollars_async = AsyncMock(
        return_value={"status": "ok", "qty": 1.0, "fill_price": 100.0}
    )
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [
            {"action": "SELL", "ticker": "ARM", "reasoning": "thesis broken"},
            {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25,
             "reasoning": "still bullish"},
        ],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    # portfolio_value = 100 (NVDA) + 2000 (ARM) + 500 cash = 2600; NVDA target 25%
    # = 650, delta = 550. Real budget is 500 only (ARM sell skipped), so the buy
    # is capped at 500 * 0.95 = 475 — NOT 550 funded by phantom ARM cash.
    mock_rh.buy_dollars_async.assert_awaited_once_with("NVDA", 475.0)


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.claude_inspection.get_record", return_value=(5, 2))
@patch("app.claude_inspection.open_position")
@patch("app.claude_inspection.close_position", return_value=(0.0, 500.0, 33.3))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_executed_sell_proceeds_still_fund_double_down(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_close_position, mock_open_position, mock_get_record,
    mock_record_rh_trade, mock_notify_private, mock_notify_public, mock_log,
):
    """Guard against over-correction: a SELL that actually executes must still
    credit its proceeds so a same-run DOUBLE_DOWN can be funded by them."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[
            {"symbol": "NVDA", "qty": 1.0, "avg_entry_price": 80.0,
             "current_price": 100.0, "unrealized_pl": 20.0, "unrealized_plpc": 25.0},
            {"symbol": "ARM", "qty": 10.0, "avg_entry_price": 150.0,
             "current_price": 200.0, "unrealized_pl": 500.0, "unrealized_plpc": 33.3},
        ]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=100.0)
    # The ARM SELL fills immediately for 10 * $200 = $2000 of proceeds.
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "ok", "qty": 10.0, "fill_price": 200.0, "queued": False}
    )
    mock_rh.buy_dollars_async = AsyncMock(
        return_value={"status": "ok", "qty": 4.5, "fill_price": 100.0}
    )
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [
            {"action": "SELL", "ticker": "ARM", "reasoning": "thesis broken"},
            {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25,
             "reasoning": "still bullish"},
        ],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    # portfolio_value = 100 + 2000 + 100 = 2200; NVDA target 25% = 550, delta = 450.
    # Budget = 100 cash + 2000 realized ARM proceeds = 2100; 2100*0.95 = 1995 >= 450,
    # so the full 450 delta is invested — the executed sell funded it.
    mock_rh.buy_dollars_async.assert_awaited_once_with("NVDA", 450.0)


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "SPY"})
@patch("app.claude_inspection.rh_client")
async def test_spy_excluded_trade_reasoning_still_captured_in_notes(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_notify_private, mock_notify_public, mock_log,
):
    """A SPY proposal is never executed, but its reasoning should still be
    recorded so next week's thesis map isn't silently missing continuity."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(
        return_value=[{"symbol": "SPY", "qty": 10.0, "avg_entry_price": 400.0,
                       "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5}]
    )
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {
        "no_changes": False,
        "trades": [{"action": "SELL", "ticker": "SPY", "reasoning": "should never execute but should be noted"}],
    }

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    logged_entry = mock_log.call_args[0][0]
    assert logged_entry["notes"]["SPY"] == "should never execute but should be noted"


def _private_embeds(mock_notify_private):
    return [c.args[0] for c in mock_notify_private.call_args_list]


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.claude_inspection.get_record", return_value=(5, 2))
@patch("app.claude_inspection.trim_position", return_value=(4.0, 200.0, 8.0))
@patch("app.claude_inspection.close_position", return_value=(5.0, 500.0, 12.5))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_filled_trades_consolidated_into_single_results_card(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_close, mock_trim, mock_get_record, mock_record_rh_trade,
    mock_notify_private, mock_notify_public, mock_log,
):
    """Hybrid batching (#4): two immediately-filled actions must NOT each post
    their own trade embed — they roll into the single COMPLETE results card."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[
        {"symbol": "NOW", "qty": 5.0, "avg_entry_price": 900.0,
         "current_price": 950.0, "unrealized_pl": 250.0, "unrealized_plpc": 5.5},
        {"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 400.0,
         "current_price": 450.0, "unrealized_pl": 500.0, "unrealized_plpc": 12.5},
    ])
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "ok", "qty": 5.0, "fill_price": 960.0, "queued": False})
    mock_rh.sell_shares_async = AsyncMock(
        return_value={"status": "ok", "qty": 4.0, "fill_price": 455.0, "queued": False})
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {"no_changes": False, "trades": [
        {"action": "SELL", "ticker": "NOW", "reasoning": "guidance cut"},
        {"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 20, "reasoning": "trim concentration"},
    ]}

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    embeds = _private_embeds(mock_notify_private)
    titles = [e.get("title", "") for e in embeds]

    # No standalone per-trade card for the filled SELL/TRIM.
    assert not any(t.startswith("🔴 KIMI SELL") or t.startswith("✂️ KIMI TRIM") for t in titles)

    # Exactly one results card, carrying one field per filled trade.
    complete = [e for e in embeds if "INSPECTION COMPLETE" in e.get("title", "")]
    assert len(complete) == 1
    card = complete[0]
    assert len(card["fields"]) == 2
    assert "Acted 2" in card["description"]
    assert "Reviewed 2" in card["description"]

    # KI Server gets exactly one roll-up decisions card mentioning both tickers,
    # now carrying each decision's reasoning (the "why") — but no dollar amounts.
    assert mock_notify_public.await_count == 1
    summary = mock_notify_public.call_args[0][0]
    assert "NOW" in summary and "NVDA" in summary
    assert "guidance cut" in summary and "trim concentration" in summary
    assert "$" not in summary  # dollars/P&L stay Private


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.claude_inspection.get_record", return_value=(5, 2))
@patch("app.claude_inspection.close_position", return_value=(5.0, 500.0, 12.5))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NVDA"})
@patch("app.claude_inspection.rh_client")
async def test_results_card_reports_held_count(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_close, mock_get_record, mock_record_rh_trade,
    mock_notify_private, mock_notify_public, mock_log,
):
    """#3: with 3 holdings reviewed and 1 acted, the card reports Held 2."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[
        {"symbol": "NOW", "qty": 5.0, "avg_entry_price": 900.0,
         "current_price": 950.0, "unrealized_pl": 250.0, "unrealized_plpc": 5.5},
        {"symbol": "MSFT", "qty": 20.0, "avg_entry_price": 280.0,
         "current_price": 300.0, "unrealized_pl": 400.0, "unrealized_plpc": 7.1},
        {"symbol": "AAPL", "qty": 15.0, "avg_entry_price": 180.0,
         "current_price": 190.0, "unrealized_pl": 150.0, "unrealized_plpc": 5.6},
    ])
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "ok", "qty": 5.0, "fill_price": 960.0, "queued": False})
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {"no_changes": False, "trades": [
        {"action": "HOLD", "ticker": "MSFT"},
        {"action": "HOLD", "ticker": "AAPL"},
        {"action": "SELL", "ticker": "NOW", "reasoning": "guidance cut"},
    ]}

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    card = next(e for e in _private_embeds(mock_notify_private) if "INSPECTION COMPLETE" in e.get("title", ""))
    assert "Reviewed 3" in card["description"]
    assert "Held 2" in card["description"]
    assert "Acted 1" in card["description"]


@pytest.mark.asyncio
@patch("app.claude_inspection._append_inspection_log")
@patch("app.claude_inspection.notify_claude_signal_feed", new_callable=AsyncMock)
@patch("app.claude_inspection.notify_claude_manager_embed", new_callable=AsyncMock)
@patch("app.rh_trade_record.record_rh_trade", new_callable=AsyncMock)
@patch("app.claude_inspection.get_record", return_value=(5, 2))
@patch("app.claude_inspection.close_position", return_value=(5.0, 500.0, 12.5))
@patch("app.claude_inspection._parse_inspection_trade_block")
@patch("app.claude_inspection._call_claude_inspection_sync")
@patch("app.claude_inspection._load_recent_inspection_entries", return_value=[])
@patch("app.claude_inspection._fetch_technical_data", return_value={})
@patch("app.claude_inspection._fetch_yf_data", return_value={"ticker": "NOW"})
@patch("app.claude_inspection.rh_client")
async def test_empty_reasoning_note_does_not_promise_analysis_in_discord(
    mock_rh, mock_yf, mock_tech, mock_history, mock_call, mock_parse,
    mock_close, mock_get_record, mock_record_rh_trade,
    mock_notify_private, mock_notify_public, mock_log,
):
    """#1: a trade with no reasoning must not leave a note claiming a full
    analysis was posted to Discord — Inspection never posts prose."""
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[
        {"symbol": "NOW", "qty": 5.0, "avg_entry_price": 900.0,
         "current_price": 950.0, "unrealized_pl": 250.0, "unrealized_plpc": 5.5},
    ])
    mock_rh.get_buying_power_async = AsyncMock(return_value=0.0)
    mock_rh.close_ticker_async = AsyncMock(
        return_value={"status": "ok", "qty": 5.0, "fill_price": 960.0, "queued": False})
    mock_call.return_value = "```json\n{}\n```"
    mock_parse.return_value = {"no_changes": False, "trades": [
        {"action": "SELL", "ticker": "NOW"},  # no reasoning
    ]}

    from app.claude_inspection import run_weekly_inspection
    await run_weekly_inspection()

    note = mock_log.call_args[0][0]["notes"]["NOW"]
    assert "see full analysis in Discord" not in note
