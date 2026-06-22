import json

import pytest


def test_load_investors_returns_empty_list_when_file_missing(tmp_path):
    from unittest.mock import patch
    from app.investors import load_investors
    # patch _REPO_FILE so migration doesn't pick up the real investors.json
    with patch("app.investors._REPO_FILE", tmp_path / "no_repo.json"):
        result = load_investors(path=tmp_path / "missing.json")
    assert result == []


def test_load_investors_parses_name_and_deposits(tmp_path):
    from app.investors import load_investors
    data = {
        "investors": [
            {
                "name": "Moses",
                "deposits": [
                    {"amount": 300.0, "entry_spy": 707.116, "date": "2026-05-09"}
                ],
            }
        ]
    }
    f = tmp_path / "investors.json"
    f.write_text(json.dumps(data))
    result = load_investors(path=f)
    assert len(result) == 1
    assert result[0].name == "Moses"
    assert result[0].deposits[0].amount == 300.0
    assert result[0].deposits[0].entry_spy == 707.116
    assert result[0].deposits[0].date == "2026-05-09"


def test_save_and_reload_roundtrip(tmp_path):
    from app.investors import Deposit, Investor, load_investors, save_investors
    investors = [
        Investor(
            name="Moses",
            deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")],
        )
    ]
    path = tmp_path / "investors.json"
    save_investors(investors, path=path)
    loaded = load_investors(path=path)
    assert loaded[0].name == "Moses"
    assert loaded[0].deposits[0].amount == 300.0
    assert loaded[0].deposits[0].entry_spy == 707.116
    assert loaded[0].deposits[0].date == "2026-05-09"


def test_save_preserves_multiple_investors(tmp_path):
    from app.investors import Deposit, Investor, load_investors, save_investors
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")]),
        Investor(name="David", deposits=[Deposit(amount=2000.0, entry_spy=710.6993, date="2026-05-09")]),
    ]
    path = tmp_path / "investors.json"
    save_investors(investors, path=path)
    loaded = load_investors(path=path)
    assert len(loaded) == 2
    assert loaded[1].name == "David"


def test_load_investors_raises_on_malformed_json(tmp_path):
    from app.investors import load_investors
    bad = tmp_path / "investors.json"
    bad.write_text("not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        load_investors(path=bad)


def test_compute_nav_per_unit_single_investor():
    from app.investors import Deposit, Investor, compute_nav_per_unit
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=600.0, date="2026-01-01")])
    ]
    # net_units = 300/600 = 0.5; real equity = 350 -> nav_per_unit = 700
    assert compute_nav_per_unit(investors, real_total_equity=350.0) == pytest.approx(700.0)


def test_compute_nav_per_unit_sums_units_across_investors():
    from app.investors import Deposit, Investor, compute_nav_per_unit
    investors = [
        Investor(name="A", deposits=[Deposit(amount=300.0, entry_spy=600.0, date="2026-01-01")]),  # 0.5 units
        Investor(name="B", deposits=[Deposit(amount=600.0, entry_spy=600.0, date="2026-01-01")]),  # 1.0 units
    ]
    # total units = 1.5; real equity = 1500 -> nav_per_unit = 1000
    assert compute_nav_per_unit(investors, real_total_equity=1500.0) == pytest.approx(1000.0)


def test_compute_nav_per_unit_returns_zero_when_no_units_outstanding():
    from app.investors import compute_nav_per_unit
    assert compute_nav_per_unit([], real_total_equity=5000.0) == 0.0


def test_compute_breakdown_single_deposit():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    result = compute_breakdown(investors, spy_price=600.0)
    assert result.investors[0].current_equity == pytest.approx(360.0)
    assert result.investors[0].total_deposited == pytest.approx(300.0)
    assert result.investors[0].dollar_pnl == pytest.approx(60.0)
    assert result.investors[0].pct_pnl == pytest.approx(20.0)
    assert result.investors[0].portfolio_share == pytest.approx(100.0)


def test_compute_breakdown_portfolio_share_splits_evenly():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
        Investor(name="B", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
    ]
    result = compute_breakdown(investors, spy_price=110.0)
    assert result.investors[0].portfolio_share == pytest.approx(50.0)
    assert result.investors[1].portfolio_share == pytest.approx(50.0)


def test_compute_breakdown_multiple_deposits_per_investor():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(
            name="Moses",
            deposits=[
                Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01"),
                Deposit(amount=500.0, entry_spy=600.0, date="2026-06-01"),
            ],
        )
    ]
    # First deposit:  300 * 600/500 = 360.0
    # Second deposit: 500 * 600/600 = 500.0
    result = compute_breakdown(investors, spy_price=600.0)
    assert result.investors[0].current_equity == pytest.approx(860.0)
    assert result.investors[0].total_deposited == pytest.approx(800.0)
    assert result.investors[0].dollar_pnl == pytest.approx(60.0)


def test_compute_breakdown_totals():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
        Investor(name="B", deposits=[Deposit(amount=2000.0, entry_spy=100.0, date="2026-01-01")]),
    ]
    result = compute_breakdown(investors, spy_price=110.0)
    assert result.total_deposited == pytest.approx(3000.0)
    assert result.total_portfolio == pytest.approx(3300.0)
    assert result.overall_dollar_pnl == pytest.approx(300.0)
    assert result.overall_pct_pnl == pytest.approx(10.0)
    assert result.spy_price == 110.0


def test_format_discord_message_contains_investor_name_and_date():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "Moses" in msg
    assert "May 9, 2026" in msg


def test_format_discord_message_shows_current_equity():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "360.00" in msg  # current_equity = 300 * 600/500


def test_format_discord_message_prefixes_positive_pnl_with_plus():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "+$60.00" in msg


def test_format_discord_message_shows_totals():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
        Investor(name="B", deposits=[Deposit(amount=2000.0, entry_spy=100.0, date="2026-01-01")]),
    ]
    breakdown = compute_breakdown(investors, spy_price=110.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "3,300.00" in msg  # total portfolio
    assert "3,000.00" in msg  # total deposited


def test_config_discord_investors_webhook_url_defaults_to_none():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x")
    assert s.discord_investors_webhook_url is None


def test_config_accepts_discord_investors_webhook_url():
    from app.config import Settings
    s = Settings(
        alpaca_api_key="x",
        alpaca_secret_key="x",
        webhook_secret="x",
        discord_investors_webhook_url="https://discord.com/api/webhooks/999/abc",
    )
    assert s.discord_investors_webhook_url == "https://discord.com/api/webhooks/999/abc"


@pytest.mark.asyncio
async def test_notify_investors_posts_to_investors_webhook():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_investors_webhook_url = "https://discord.com/investors"
        mock_settings.discord_webhook_url = "https://discord.com/main"
        from app.notifications import notify_investors
        await notify_investors("test message")
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://discord.com/investors"


@pytest.mark.asyncio
async def test_notify_investors_falls_back_to_main_webhook():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_investors_webhook_url = None
        mock_settings.discord_webhook_url = "https://discord.com/main"
        from app.notifications import notify_investors
        await notify_investors("test message")
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://discord.com/main"


@pytest.mark.asyncio
async def test_notify_investors_skips_when_no_webhooks_set():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_investors_webhook_url = None
        mock_settings.discord_webhook_url = None
        from app.notifications import notify_investors
        await notify_investors("test message")
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_investors_with_chart_posts_file_to_investors_webhook():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_investors_webhook_url = "https://discord.com/investors"
        mock_settings.discord_webhook_url = "https://discord.com/main"
        from app.notifications import notify_investors_with_chart
        await notify_investors_with_chart("test message", b"\x89PNG_fake")
    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://discord.com/investors"
    assert kwargs["files"]["file"][1] == b"\x89PNG_fake"


@pytest.mark.asyncio
async def test_notify_investors_with_chart_falls_back_to_main_webhook():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_investors_webhook_url = None
        mock_settings.discord_webhook_url = "https://discord.com/main"
        from app.notifications import notify_investors_with_chart
        await notify_investors_with_chart("test message", b"\x89PNG_fake")
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://discord.com/main"


@pytest.mark.asyncio
async def test_notify_investors_with_chart_sends_text_only_when_no_chart_bytes():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_investors_webhook_url = "https://discord.com/investors"
        mock_settings.discord_webhook_url = "https://discord.com/main"
        from app.notifications import notify_investors_with_chart
        await notify_investors_with_chart("test message", b"")
    mock_client.post.assert_called_once()
    _, kwargs = mock_client.post.call_args
    assert "files" not in kwargs
    assert kwargs["json"] == {"content": "test message"}


@pytest.mark.asyncio
async def test_notify_investors_with_chart_skips_when_no_webhooks_set():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_investors_webhook_url = None
        mock_settings.discord_webhook_url = None
        from app.notifications import notify_investors_with_chart
        await notify_investors_with_chart("test message", b"\x89PNG_fake")
    mock_client.post.assert_not_called()


def test_deposit_request_valid_without_spy_price():
    from app.models import DepositRequest
    req = DepositRequest(secret="s", investor="Moses", amount=500.0)
    assert req.spy_price is None
    assert req.amount == 500.0
    assert req.investor == "Moses"


def test_deposit_request_valid_with_explicit_spy_price():
    from app.models import DepositRequest
    req = DepositRequest(secret="s", investor="Moses", amount=500.0, spy_price=580.0)
    assert req.spy_price == 580.0


def test_deposit_request_rejects_zero_amount():
    from pydantic import ValidationError
    from app.models import DepositRequest
    with pytest.raises(ValidationError):
        DepositRequest(secret="s", investor="Moses", amount=0.0)


def test_deposit_request_rejects_negative_amount():
    from pydantic import ValidationError
    from app.models import DepositRequest
    with pytest.raises(ValidationError):
        DepositRequest(secret="s", investor="Moses", amount=-100.0)


@pytest.mark.asyncio
async def test_send_investor_report_skips_when_no_investors():
    from unittest.mock import AsyncMock, patch
    with patch("app.pnl.load_investors", return_value=[]):
        with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
            from app.pnl import send_investor_report
            await send_investor_report()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_send_investor_report_skips_when_spy_price_unavailable():
    from unittest.mock import AsyncMock, patch
    from app.investors import Deposit, Investor
    mock_investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    with patch("app.pnl.load_investors", return_value=mock_investors):
        with patch("app.pnl.get_latest_price", return_value=None):
            with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
                from app.pnl import send_investor_report
                await send_investor_report()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_send_investor_report_sends_chart_with_investor_name():
    from unittest.mock import AsyncMock, patch
    from app.investors import Deposit, Investor
    mock_investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    with patch("app.pnl.load_investors", return_value=mock_investors):
        with patch("app.pnl.get_latest_price", return_value=600.0):
            with patch("app.pnl.generate_investor_pie_chart", return_value=b"\x89PNG_fake"):
                with patch("app.pnl.notify_investors_with_chart", new_callable=AsyncMock) as mock_notify_chart:
                    with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
                        from app.pnl import send_investor_report
                        await send_investor_report()
    mock_notify_chart.assert_called_once()
    mock_notify.assert_not_called()
    message, chart_bytes = mock_notify_chart.call_args[0]
    assert "Moses" in message
    assert "360.00" in message  # 300 * 600/500
    assert chart_bytes == b"\x89PNG_fake"


@pytest.mark.asyncio
async def test_send_investor_report_falls_back_to_text_when_no_chart():
    from unittest.mock import AsyncMock, patch
    from app.investors import Deposit, Investor
    mock_investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    with patch("app.pnl.load_investors", return_value=mock_investors):
        with patch("app.pnl.get_latest_price", return_value=600.0):
            with patch("app.pnl.generate_investor_pie_chart", return_value=b""):
                with patch("app.pnl.notify_investors_with_chart", new_callable=AsyncMock) as mock_notify_chart:
                    with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
                        from app.pnl import send_investor_report
                        await send_investor_report()
    mock_notify_chart.assert_not_called()
    mock_notify.assert_called_once()
    message = mock_notify.call_args[0][0]
    assert "Moses" in message
    assert "360.00" in message  # 300 * 600/500


def test_setup_jobs_registers_parallel_bundles():
    from unittest.mock import MagicMock, patch
    mock_scheduler = MagicMock()
    with patch("app.scheduler.scheduler", mock_scheduler):
        from app.scheduler import setup_jobs
        setup_jobs()
    registered_ids = [call.kwargs.get("id") for call in mock_scheduler.add_job.call_args_list]
    assert "weekday_jobs" in registered_ids
    assert "friday_jobs" in registered_ids


def test_serialize_investors_returns_valid_json():
    from app.investors import Deposit, Investor, serialize_investors
    import json
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")])
    ]
    result = serialize_investors(investors)
    data = json.loads(result)
    assert data["investors"][0]["name"] == "Moses"
    assert data["investors"][0]["deposits"][0]["amount"] == 300.0
    assert data["investors"][0]["deposits"][0]["entry_spy"] == 707.116


def test_serialize_investors_output_matches_save_investors(tmp_path):
    from app.investors import Deposit, Investor, save_investors, serialize_investors
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")])
    ]
    path = tmp_path / "investors.json"
    save_investors(investors, path=path)
    assert path.read_text(encoding="utf-8") == serialize_investors(investors)


def test_config_github_token_defaults_to_none():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x")
    assert s.github_token is None


def test_config_github_repo_defaults_to_auto_trade():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x")
    assert s.github_repo == "Moses-log/Auto-Trade"


def test_config_github_token_accepts_value():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x", github_token="ghp_abc123")
    assert s.github_token == "ghp_abc123"


def test_config_discord_trades_webhook_url_defaults_to_none():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x")
    assert s.discord_trades_webhook_url is None


def test_config_accepts_discord_trades_webhook_url():
    from app.config import Settings
    s = Settings(
        alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x",
        discord_trades_webhook_url="https://discord.com/api/webhooks/trades/abc",
    )
    assert s.discord_trades_webhook_url == "https://discord.com/api/webhooks/trades/abc"


@pytest.mark.asyncio
async def test_notify_trades_posts_to_trades_webhook():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_trades_webhook_url = "https://discord.com/trades"
        from app.notifications import notify_trades
        await notify_trades("test trade message")
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://discord.com/trades"


@pytest.mark.asyncio
async def test_notify_trades_skips_when_url_not_set():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_trades_webhook_url = None
        from app.notifications import notify_trades
        await notify_trades("test trade message")
    mock_client.post.assert_not_called()


def test_get_total_deposited_sums_all_deposits():
    from app.investors import Investor, Deposit, get_total_deposited
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=707.0, date="2026-05-09"),
        Deposit(amount=200.0, entry_spy=720.0, date="2026-05-10"),
    ])
    assert get_total_deposited(inv) == 500.0


def test_get_total_deposited_handles_withdrawals():
    from app.investors import Investor, Deposit, get_total_deposited
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09"),
        Deposit(amount=-500.0, entry_spy=741.0, date="2026-05-16"),
    ])
    assert get_total_deposited(inv) == 1500.0


def test_time_weighted_capital_full_year_holding():
    """Capital deposited before the year and held throughout counts at full value."""
    from app.investors import Investor, Deposit, compute_time_weighted_capital
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=1000.0, entry_spy=600.0, date="2025-06-01"),
    ])
    assert compute_time_weighted_capital(inv, 2026) == pytest.approx(1000.0)


def test_time_weighted_capital_mid_year_deposit_counts_partial_year():
    """An investor who joined mid-year should get less than their full deposit as their
    time-weighted share, since they weren't invested for the whole year."""
    from app.investors import Investor, Deposit, compute_time_weighted_capital
    inv = Investor(name="Alex", deposits=[
        Deposit(amount=1000.0, entry_spy=600.0, date="2026-07-02"),  # joins exactly halfway
    ])
    result = compute_time_weighted_capital(inv, 2026)
    assert 0 < result < 1000.0
    assert result == pytest.approx(500.0, rel=0.02)


def test_time_weighted_capital_full_withdrawal_mid_year_retains_prior_share():
    """An investor who withdrew everything mid-year should still get credit for the
    capital they had at stake before the withdrawal — not zero."""
    from app.investors import Investor, Deposit, compute_time_weighted_capital
    inv = Investor(name="Departed", deposits=[
        Deposit(amount=1000.0, entry_spy=600.0, date="2025-01-01"),
        Deposit(amount=-1000.0, entry_spy=650.0, date="2026-07-02"),  # withdraws halfway through 2026
    ])
    result = compute_time_weighted_capital(inv, 2026)
    assert result == pytest.approx(500.0, rel=0.02)
    # And by the following year, they hold nothing.
    assert compute_time_weighted_capital(inv, 2027) == 0.0


def test_time_weighted_capital_no_deposits_in_year_is_zero():
    from app.investors import Investor, Deposit, compute_time_weighted_capital
    inv = Investor(name="Future", deposits=[
        Deposit(amount=1000.0, entry_spy=600.0, date="2027-01-15"),
    ])
    assert compute_time_weighted_capital(inv, 2026) == 0.0


@pytest.mark.asyncio
async def test_notify_with_chart_posts_multipart_to_webhook():
    from unittest.mock import AsyncMock, patch
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_webhook_url = "https://discord.com/api/webhooks/test"
        from app.notifications import notify_with_chart
        await notify_with_chart("P&L message", b"fake_png")
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args[1]
    assert "files" in call_kwargs
    assert "data" in call_kwargs


@pytest.mark.asyncio
async def test_notify_with_chart_skips_when_no_webhook():
    from unittest.mock import patch, AsyncMock
    mock_client = AsyncMock()
    with patch("app.notifications.settings") as mock_settings, \
         patch("app.notifications._client", mock_client):
        mock_settings.discord_webhook_url = None
        from app.notifications import notify_with_chart
        await notify_with_chart("msg", b"bytes")
    mock_client.post.assert_not_called()
