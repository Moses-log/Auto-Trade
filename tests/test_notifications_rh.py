import os
os.environ.setdefault("ALPACA_API_KEY",    "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET",    "MY_SHARED_SECRET")

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

@pytest.mark.asyncio
async def test_notify_robinhood_uses_rh_url():
    """Posts to RH_DISCORD_WEBHOOK_URL when set."""
    with patch("app.notifications.settings") as mock_settings, \
         patch("httpx.AsyncClient") as mock_client_class:
        mock_settings.rh_discord_webhook_url = "https://discord.com/rh-channel"
        mock_settings.discord_webhook_url = "https://discord.com/main-channel"

        mock_response = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_class.return_value = mock_client

        from app.notifications import notify_robinhood
        await notify_robinhood("test message")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://discord.com/rh-channel"


@pytest.mark.asyncio
async def test_notify_robinhood_falls_back_to_main_url():
    """Falls back to main DISCORD_WEBHOOK_URL when RH url is not set."""
    with patch("app.notifications.settings") as mock_settings, \
         patch("httpx.AsyncClient") as mock_client_class:
        mock_settings.rh_discord_webhook_url = None
        mock_settings.discord_webhook_url = "https://discord.com/main-channel"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_client_class.return_value = mock_client

        from app.notifications import notify_robinhood
        await notify_robinhood("test message")

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://discord.com/main-channel"


@pytest.mark.asyncio
async def test_notify_robinhood_silent_when_no_urls():
    """Does nothing (no-op) when neither URL is set."""
    with patch("app.notifications.settings") as mock_settings, \
         patch("httpx.AsyncClient") as mock_client_class:
        mock_settings.rh_discord_webhook_url = None
        mock_settings.discord_webhook_url = None

        from app.notifications import notify_robinhood
        await notify_robinhood("test message")  # should not raise

        mock_client_class.assert_not_called()
