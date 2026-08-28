"""Tests for the fund-level 'Non-SPY realized P&L today' feature:
realized_pnl_today() in the record module and its rendering in the
investor breakdown footer.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    import app.alpaca_hf_record as rec
    monkeypatch.setattr(rec, "_STATE_FILE", tmp_path / "hf.json")
    yield


@pytest.mark.asyncio
async def test_realized_pnl_today_sums_only_today():
    import app.alpaca_hf_record as rec

    now = datetime.now(timezone.utc)
    yday = now - timedelta(days=1)

    # Two closes today, one yesterday. record_close stores closed_ts verbatim,
    # so pass ISO timestamps directly.
    await rec.record_open("QCOM", "LONG", 12, 164.37, "o", "q1")
    await rec.record_close("QCOM", "LONG", 12, 164.84, now.isoformat())      # +5.64 today
    await rec.record_open("TSM", "LONG", 4, 425.47, "o", "t1")
    await rec.record_close("TSM", "LONG", 4, 426.55, now.isoformat())        # +4.32 today
    await rec.record_open("OLD", "LONG", 1, 100.0, "o", "old1")
    await rec.record_close("OLD", "LONG", 1, 200.0, yday.isoformat())        # +100 yesterday

    today_total = await rec.realized_pnl_today(tz="UTC")
    assert round(today_total, 2) == round(5.64 + 4.32, 2)


@pytest.mark.asyncio
async def test_realized_pnl_today_zero_when_none_today():
    import app.alpaca_hf_record as rec
    assert await rec.realized_pnl_today(tz="UTC") == 0.0


def test_footer_line_renders_when_provided():
    from app.investors import (
        InvestorBreakdown,
        InvestorResult,
        format_discord_message,
    )

    r = InvestorResult(
        name="Alice", total_deposited=1000.0, current_equity=1100.0,
        dollar_pnl=100.0, pct_pnl=10.0, portfolio_share=100.0,
    )
    breakdown = InvestorBreakdown(
        investors=[r], spy_price=500.0, total_portfolio=1100.0,
        total_deposited=1000.0, overall_dollar_pnl=100.0, overall_pct_pnl=10.0,
    )

    msg = format_discord_message(breakdown, "August 27, 2026", nonspy_today=12.34)
    assert "Non-SPY realized P&L today: +$12.34" in msg

    msg_neg = format_discord_message(breakdown, "August 27, 2026", nonspy_today=-8.90)
    assert "Non-SPY realized P&L today: -$8.90" in msg_neg

    # Omitting the arg keeps the old output (no footer line).
    msg_none = format_discord_message(breakdown, "August 27, 2026")
    assert "realized P&L today" not in msg_none
