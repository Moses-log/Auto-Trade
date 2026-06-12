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


def _fake_breakdown():
    from app.investors import InvestorBreakdown, InvestorResult
    return InvestorBreakdown(
        investors=[
            InvestorResult(
                name="Moses", total_deposited=300.0, current_equity=360.0,
                dollar_pnl=60.0, pct_pnl=20.0, portfolio_share=60.0,
            ),
            InvestorResult(
                name="Alex", total_deposited=200.0, current_equity=240.0,
                dollar_pnl=40.0, pct_pnl=20.0, portfolio_share=40.0,
            ),
        ],
        spy_price=600.0,
        total_portfolio=600.0,
        total_deposited=500.0,
        overall_dollar_pnl=100.0,
        overall_pct_pnl=20.0,
    )


def test_generate_investor_pie_chart_returns_png_bytes():
    from app.chart import generate_investor_pie_chart
    result = generate_investor_pie_chart(_fake_breakdown(), "June 10, 2026")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC


def test_generate_investor_pie_chart_empty_when_no_portfolio_value():
    from app.chart import generate_investor_pie_chart
    from app.investors import InvestorBreakdown
    breakdown = InvestorBreakdown(
        investors=[],
        spy_price=600.0,
        total_portfolio=0.0,
        total_deposited=0.0,
        overall_dollar_pnl=0.0,
        overall_pct_pnl=0.0,
    )
    result = generate_investor_pie_chart(breakdown, "June 10, 2026")
    assert result == b""


def test_generate_investor_pie_chart_excludes_negative_equity_investor():
    """An investor who withdrew more than their current equity is worth can end
    up with negative current_equity. The chart should skip them rather than
    crash, and the remaining wedges/percentages should stay self-consistent.
    """
    from app.chart import generate_investor_pie_chart
    from app.investors import InvestorBreakdown, InvestorResult
    breakdown = InvestorBreakdown(
        investors=[
            InvestorResult(
                name="Moses", total_deposited=300.0, current_equity=600.0,
                dollar_pnl=300.0, pct_pnl=100.0, portfolio_share=120.0,
            ),
            InvestorResult(
                name="Alex", total_deposited=0.0, current_equity=-100.0,
                dollar_pnl=-100.0, pct_pnl=0.0, portfolio_share=-20.0,
            ),
        ],
        spy_price=600.0,
        total_portfolio=500.0,
        total_deposited=300.0,
        overall_dollar_pnl=200.0,
        overall_pct_pnl=66.7,
    )
    result = generate_investor_pie_chart(breakdown, "June 10, 2026")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC


def test_generate_investor_pie_chart_handles_more_than_palette_colors():
    """13+ investors should still render without IndexError (colors wrap via modulo)."""
    from app.chart import generate_investor_pie_chart
    from app.investors import InvestorBreakdown, InvestorResult
    n = 13
    investors = [
        InvestorResult(
            name=f"Investor{i}", total_deposited=100.0, current_equity=100.0,
            dollar_pnl=0.0, pct_pnl=0.0, portfolio_share=100.0 / n,
        )
        for i in range(n)
    ]
    breakdown = InvestorBreakdown(
        investors=investors,
        spy_price=600.0,
        total_portfolio=100.0 * n,
        total_deposited=100.0 * n,
        overall_dollar_pnl=0.0,
        overall_pct_pnl=0.0,
    )
    result = generate_investor_pie_chart(breakdown, "June 10, 2026")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC
