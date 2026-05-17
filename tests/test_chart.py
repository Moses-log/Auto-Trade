import os
import time
import pytest
import pandas as pd

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

_PNG_MAGIC = b"\x89PNG"


def _fake_spy_df():
    dates = pd.date_range(start="2026-05-09", periods=4, freq="D")
    return pd.DataFrame({"Close": [530.0, 533.0, 536.0, 538.0]}, index=dates)


def _fake_timestamps(n=4):
    now = int(time.time())
    return [now - 86400 * (n - 1 - i) for i in range(n)]


def test_generate_equity_chart_returns_png_bytes():
    from app.chart import generate_equity_chart
    equity = [10000.0, 10100.0, 10250.0, 10320.0]
    timestamps = _fake_timestamps(4)
    result = generate_equity_chart(equity, timestamps, _fake_spy_df(), "Test Chart")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC


def test_generate_equity_chart_handles_none_spy():
    from app.chart import generate_equity_chart
    equity = [10000.0, 10500.0]
    timestamps = _fake_timestamps(2)
    result = generate_equity_chart(equity, timestamps, None, "No SPY Chart")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC


def test_generate_equity_chart_handles_empty_spy_df():
    from app.chart import generate_equity_chart
    equity = [10000.0, 10200.0]
    timestamps = _fake_timestamps(2)
    result = generate_equity_chart(equity, timestamps, pd.DataFrame(), "Empty SPY")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC


def test_generate_equity_chart_single_data_point():
    from app.chart import generate_equity_chart
    equity = [10000.0]
    timestamps = [int(time.time())]
    result = generate_equity_chart(equity, timestamps, None, "Single Point")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC
