import os
import pytest
from unittest.mock import AsyncMock, patch
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


@pytest.mark.asyncio
async def test_notify_hf_trade_posts_to_webhook():
    import app.notifications as n
    client = AsyncMock()
    with patch.object(n.settings, "alpaca_hf_trades_webhook_url", "https://x/trades"), \
         patch.object(n, "get_http_client", return_value=client):
        await n.notify_hf_trade("hello")
    client.post.assert_awaited_once()
    assert client.post.await_args.args[0] == "https://x/trades"


@pytest.mark.asyncio
async def test_notify_hf_recap_noop_when_unset():
    import app.notifications as n
    client = AsyncMock()
    with patch.object(n.settings, "alpaca_hf_recap_webhook_url", None), \
         patch.object(n, "get_http_client", return_value=client):
        await n.notify_hf_recap("hello")
    client.post.assert_not_awaited()
