import os
import pytest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("DISCORD_APP_ID", "test-app-id")


@pytest.mark.asyncio
async def test_handle_deposit_success():
    fake_investor = MagicMock()
    fake_investor.name = "Moses"
    fake_investor.deposits = []

    with patch("app.discord_commands.load_investors", return_value=[fake_investor]), \
         patch("app.discord_commands.get_latest_price", return_value=741.20), \
         patch("app.discord_commands.save_investors"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_deposit
        await handle_deposit("Moses", 2000.0, None, "test-token")

    mock_edit.assert_called_once()
    msg = mock_edit.call_args[0][1]
    assert "Moses" in msg
    assert "2,000" in msg
    assert "741.20" in msg


@pytest.mark.asyncio
async def test_handle_deposit_investor_not_found():
    with patch("app.discord_commands.load_investors", return_value=[]), \
         patch("app.discord_commands.get_latest_price", return_value=741.20), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_deposit
        await handle_deposit("Ghost", 500.0, None, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "not found" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_schedules_instead_of_writing_immediately():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.get_account", return_value=SimpleNamespace(equity="2000.00")), \
         patch("app.withdrawal_execution.save_investors") as mock_save, \
         patch("app.withdrawal_execution.scheduler"), \
         patch("app.withdrawal_execution.save_pending_withdrawal"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    mock_save.assert_not_called()  # investors.json is NOT written yet
    msg = mock_edit.call_args[0][1]
    assert "500" in msg
    assert "Moses" in msg
    assert "cancel-withdrawal" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_exceeds_total_reports_error_without_scheduling():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.get_account", return_value=SimpleNamespace(equity="300.00")), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "exceeds" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_investor_not_found():
    with patch("app.withdrawal_execution.load_investors", return_value=[]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Ghost", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "not found" in msg


@pytest.mark.asyncio
async def test_handle_report_daily():
    with patch("app.discord_commands.send_daily_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "daily", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "daily" in msg.lower()


@pytest.mark.asyncio
async def test_handle_report_both():
    with patch("app.discord_commands.send_daily_report", new_callable=AsyncMock), \
         patch("app.discord_commands.send_weekly_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "both", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_deposit_spy_fetch_failure():
    fake_investor = MagicMock()
    fake_investor.name = "Moses"
    fake_investor.deposits = []

    with patch("app.discord_commands.load_investors", return_value=[fake_investor]), \
         patch("app.discord_commands.get_latest_price", return_value=None), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_deposit
        await handle_deposit("Moses", 2000.0, None, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "Could not fetch SPY price" in msg


@pytest.mark.asyncio
async def test_handle_report_monthly():
    with patch("app.discord_commands.send_monthly_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "monthly", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "monthly" in msg.lower()


@pytest.mark.asyncio
async def test_handle_report_ytd():
    with patch("app.discord_commands.send_ytd_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "ytd", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_report_1year():
    with patch("app.discord_commands.send_yearly_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "1year", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_report_alltime():
    with patch("app.discord_commands.send_alltime_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "alltime", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_report_inception():
    with patch("app.discord_commands.send_inception_report", new_callable=AsyncMock) as mock_inception, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "inception", "test-token")
    mock_inception.assert_called_once()
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_report_custom_success():
    with patch("app.discord_commands.send_custom_report", new_callable=AsyncMock) as mock_custom, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "custom", "test-token", custom_date="2026-01-15")

    mock_custom.assert_called_once()
    called_date = mock_custom.call_args[0][0]
    assert called_date == date(2026, 1, 15)
    msg = mock_edit.call_args[0][1]
    assert "2026-01-15" in msg


@pytest.mark.asyncio
async def test_handle_report_custom_missing_date():
    with patch("app.discord_commands.send_custom_report", new_callable=AsyncMock) as mock_custom, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "custom", "test-token")

    mock_custom.assert_not_called()
    msg = mock_edit.call_args[0][1]
    assert "date" in msg.lower()


@pytest.mark.asyncio
async def test_handle_report_custom_invalid_date_format():
    with patch("app.discord_commands.send_custom_report", new_callable=AsyncMock) as mock_custom, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "custom", "test-token", custom_date="01/15/2026")

    mock_custom.assert_not_called()
    msg = mock_edit.call_args[0][1]
    assert "Invalid date" in msg


@pytest.mark.asyncio
async def test_handle_report_custom_future_date():
    with patch("app.discord_commands.send_custom_report", new_callable=AsyncMock) as mock_custom, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alpaca", "custom", "test-token", custom_date="2099-01-01")

    mock_custom.assert_not_called()
    msg = mock_edit.call_args[0][1]
    assert "future" in msg.lower()


@pytest.mark.asyncio
async def test_dispatch_report_custom_subcommand_routes_to_handle_report():
    with patch("app.discord_commands.handle_report", new_callable=AsyncMock) as mock_handle:
        from app.discord_commands import dispatch_command
        await dispatch_command(
            "report",
            {"_subcommand": "custom", "date": "2026-01-15"},
            "test-token",
        )
    mock_handle.assert_called_once_with(
        broker="alpaca", report_type="custom", token="test-token", custom_date="2026-01-15",
    )


@pytest.mark.asyncio
async def test_dispatch_report_alpaca_subcommand_routes_to_handle_report():
    with patch("app.discord_commands.handle_report", new_callable=AsyncMock) as mock_handle:
        from app.discord_commands import dispatch_command
        await dispatch_command(
            "report",
            {"_subcommand": "alpaca", "type": "inception"},
            "test-token",
        )
    mock_handle.assert_called_once_with(broker="alpaca", report_type="inception", token="test-token")


@pytest.mark.asyncio
async def test_handle_report_robinhood():
    with patch("app.discord_commands.send_rh_report", new_callable=AsyncMock) as mock_rh, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("robinhood", "daily", "test-token")
    mock_rh.assert_called_once_with("daily")
    msg = mock_edit.call_args[0][1]
    assert "RH" in msg


@pytest.mark.asyncio
async def test_handle_status_alpaca_up_rh_down():
    mock_account = MagicMock()
    mock_account.equity = "52341.18"
    mock_account.cash = "12500.00"

    mock_pos = MagicMock()
    mock_pos.symbol = "SPY"
    mock_pos.qty = "10"
    mock_pos.unrealized_pl = "345.20"
    mock_pos.unrealized_plpc = "0.0264"

    mock_rh = MagicMock()
    mock_rh.available = False
    mock_rh.get_all_positions_async = AsyncMock(return_value=[])
    mock_rh.get_buying_power_async = AsyncMock(return_value=None)

    with patch("app.discord_commands.get_account", return_value=mock_account), \
         patch("app.discord_commands.get_all_positions", return_value=[mock_pos]), \
         patch("app.discord_commands.rh_client", mock_rh), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_status
        await handle_status("test-token")

    msg = mock_edit.call_args[0][1]
    assert "ALPACA" in msg
    assert "ROBINHOOD" in msg
    assert "52,341" in msg
    assert "Session Offline" in msg


@pytest.mark.asyncio
async def test_handle_status_alpaca_down():
    mock_rh = MagicMock()
    mock_rh.available = True
    mock_rh.get_all_positions_async = AsyncMock(return_value=[])
    mock_rh.get_buying_power_async = AsyncMock(return_value=3200.0)

    with patch("app.discord_commands.get_account", side_effect=Exception("connection error")), \
         patch("app.discord_commands.get_all_positions", return_value=[]), \
         patch("app.discord_commands.rh_client", mock_rh), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_status
        await handle_status("test-token")

    msg = mock_edit.call_args[0][1]
    assert "Unavailable" in msg
    assert "ROBINHOOD" in msg


@pytest.mark.asyncio
async def test_handle_positions_alpaca_only():
    mock_pos = MagicMock()
    mock_pos.symbol = "SPY"
    mock_pos.qty = "5"
    mock_pos.avg_entry_price = "520.00"
    mock_pos.current_price = "535.60"
    mock_pos.unrealized_pl = "78.00"
    mock_pos.unrealized_plpc = "0.03"

    with patch("app.discord_commands.get_all_positions", return_value=[mock_pos]), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_positions
        await handle_positions("alpaca", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "SPY" in msg
    assert "Alpaca" in msg
    assert "520.00" in msg


@pytest.mark.asyncio
async def test_handle_close_alpaca_success():
    mock_order = MagicMock()
    mock_order.qty = "10"

    mock_rh = MagicMock()
    mock_rh.available = True
    mock_rh.close_ticker_async = AsyncMock(return_value={"status": "ok", "qty": 5.0})

    with patch("app.discord_commands.close_position", return_value=mock_order), \
         patch("app.discord_commands.rh_client", mock_rh), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_close
        await handle_close("SPY", "both", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "CLOSE SPY" in msg
    assert "Alpaca" in msg
    assert "Robinhood" in msg
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_close_no_position():
    mock_rh = MagicMock()
    mock_rh.close_ticker_async = AsyncMock(return_value={"status": "ok", "note": "no position to close", "qty": 0.0})

    with patch("app.discord_commands.close_position", return_value=None), \
         patch("app.discord_commands.rh_client", mock_rh), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_close
        await handle_close("SPY", "both", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "No position to close" in msg or "no position" in msg.lower()


@pytest.mark.asyncio
async def test_handle_cancel_withdrawal_success():
    record = {"id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
              "requested_at": "t1", "run_at": "t2"}
    with patch("app.withdrawal_execution.cancel_pending_withdrawal", new_callable=AsyncMock) as mock_cancel, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        mock_cancel.return_value = record
        from app.discord_commands import handle_cancel_withdrawal
        await handle_cancel_withdrawal("wd-aaaa1111", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "Canceled" in msg
    assert "500" in msg
    assert "Moses" in msg


@pytest.mark.asyncio
async def test_handle_cancel_withdrawal_not_found():
    from app.withdrawal_execution import WithdrawalNotFoundError
    with patch("app.withdrawal_execution.cancel_pending_withdrawal", new_callable=AsyncMock) as mock_cancel, \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        mock_cancel.side_effect = WithdrawalNotFoundError("No pending withdrawal with id wd-missing")
        from app.discord_commands import handle_cancel_withdrawal
        await handle_cancel_withdrawal("wd-missing", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "No pending withdrawal" in msg


@pytest.mark.asyncio
async def test_handle_pending_withdrawals_lists_all_pending():
    records = [
        {"id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
         "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00"},
        {"id": "wd-bbbb2222", "investor": "Gabe", "amount": 200.0,
         "requested_at": "2026-06-21T11:00:00-05:00", "run_at": "2026-06-22T11:00:00-05:00"},
    ]
    with patch("app.discord_commands.load_pending_withdrawals", return_value=records), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_pending_withdrawals
        await handle_pending_withdrawals("test-token")

    msg = mock_edit.call_args[0][1]
    assert "wd-aaaa1111" in msg
    assert "Moses" in msg
    assert "wd-bbbb2222" in msg
    assert "Gabe" in msg


@pytest.mark.asyncio
async def test_handle_pending_withdrawals_reports_none_pending():
    with patch("app.discord_commands.load_pending_withdrawals", return_value=[]), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_pending_withdrawals
        await handle_pending_withdrawals("test-token")

    msg = mock_edit.call_args[0][1]
    assert "No pending withdrawals" in msg
