import os
import pytest
from datetime import date
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
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_deposit
        await handle_deposit("Ghost", 500.0, None, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "not found" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_success():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.discord_commands.load_investors", return_value=[inv]), \
         patch("app.discord_commands.get_latest_price", return_value=741.20), \
         patch("app.discord_commands.save_investors"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "500" in msg
    assert "Moses" in msg
    assert "1,500" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_exceeds_total():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.discord_commands.load_investors", return_value=[inv]), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "exceeds" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_investor_not_found():
    with patch("app.discord_commands.load_investors", return_value=[]), \
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
        await handle_report("daily", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "daily" in msg.lower()


@pytest.mark.asyncio
async def test_handle_report_both():
    with patch("app.discord_commands.send_daily_report", new_callable=AsyncMock), \
         patch("app.discord_commands.send_weekly_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("both", "test-token")

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
        await handle_report("monthly", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "monthly" in msg.lower()


@pytest.mark.asyncio
async def test_handle_report_ytd():
    with patch("app.discord_commands.send_ytd_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("ytd", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_report_1year():
    with patch("app.discord_commands.send_yearly_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("1year", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_report_alltime():
    with patch("app.discord_commands.send_alltime_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alltime", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg
