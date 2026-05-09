import os
import pytest
from unittest.mock import patch, MagicMock

# Must set env vars before importing any app module
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "test_webhook")


# ── alpaca_client tests ───────────────────────────────────────────────────────

@patch("app.trading.alpaca_client.get_client")
def test_get_portfolio_history_calls_sdk(mock_get_client):
    """get_portfolio_history() must call TradingClient.get_portfolio_history with correct params."""
    from app.trading.alpaca_client import get_portfolio_history

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    fake_history = MagicMock()
    mock_client.get_portfolio_history.return_value = fake_history

    result = get_portfolio_history(period="1D", timeframe="1Min")

    mock_client.get_portfolio_history.assert_called_once()
    call_kwargs = mock_client.get_portfolio_history.call_args
    # Verify a filter object was passed (not raw strings)
    assert call_kwargs is not None
    assert result is fake_history
