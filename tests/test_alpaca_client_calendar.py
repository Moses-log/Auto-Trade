import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest


@patch("app.trading.alpaca_client.date")
def test_true_when_period_start_is_today_or_future(mock_date):
    from app.trading.alpaca_client import is_first_trading_day_of
    mock_date.today.return_value = date(2026, 7, 1)
    assert is_first_trading_day_of(date(2026, 7, 1)) is True
    assert is_first_trading_day_of(date(2026, 7, 5)) is True


@patch("app.trading.alpaca_client.get_client")
@patch("app.trading.alpaca_client.date")
def test_true_when_no_prior_trading_day_in_range(mock_date, mock_get_client):
    from app.trading.alpaca_client import is_first_trading_day_of
    mock_date.today.return_value = date(2026, 7, 3)  # e.g. Jul 1-2 were a holiday weekend
    mock_client = MagicMock()
    mock_client.get_calendar.return_value = []
    mock_get_client.return_value = mock_client
    assert is_first_trading_day_of(date(2026, 7, 1)) is True


@patch("app.trading.alpaca_client.get_client")
@patch("app.trading.alpaca_client.date")
def test_false_when_a_prior_trading_day_exists(mock_date, mock_get_client):
    from app.trading.alpaca_client import is_first_trading_day_of
    mock_date.today.return_value = date(2026, 7, 3)
    mock_client = MagicMock()
    mock_client.get_calendar.return_value = [MagicMock()]  # Jul 1st or 2nd already traded
    mock_get_client.return_value = mock_client
    assert is_first_trading_day_of(date(2026, 7, 1)) is False


@patch("app.trading.alpaca_client.get_client")
@patch("app.trading.alpaca_client.date")
def test_defaults_true_on_error(mock_date, mock_get_client):
    from app.trading.alpaca_client import is_first_trading_day_of
    mock_date.today.return_value = date(2026, 7, 3)
    mock_get_client.side_effect = Exception("API down")
    assert is_first_trading_day_of(date(2026, 7, 1)) is True
