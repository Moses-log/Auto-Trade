import os
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


# ── RH tax summary ────────────────────────────────────────────────────────────

def test_rh_tax_summary_basic():
    trades = [
        {"ts": "2025-03-10T14:00:00+00:00", "ticker": "SPY", "dollar_pnl": 100.0, "is_win": True},
        {"ts": "2025-05-15T14:00:00+00:00", "ticker": "SPY", "dollar_pnl": -40.0, "is_win": False},
        {"ts": "2025-08-20T14:00:00+00:00", "ticker": "SPY", "dollar_pnl": 200.0, "is_win": True},
        {"ts": "2024-12-31T23:59:00+00:00", "ticker": "SPY", "dollar_pnl": 999.0, "is_win": True},  # prior year
    ]
    with patch("app.rh_trade_record.get_all_trades", return_value=trades):
        from app.tax import compute_rh_tax_summary
        result = compute_rh_tax_summary(2025)

    assert result["total_trades"] == 3
    assert result["win_count"] == 2
    assert result["loss_count"] == 1
    assert abs(result["short_term_gains"] - 300.0) < 0.01
    assert abs(result["short_term_losses"] - (-40.0)) < 0.01
    assert abs(result["short_term_net"] - 260.0) < 0.01


def test_rh_tax_summary_no_trades():
    with patch("app.rh_trade_record.get_all_trades", return_value=[]):
        from app.tax import compute_rh_tax_summary
        result = compute_rh_tax_summary(2025)

    assert result["total_trades"] == 0
    assert result["short_term_net"] == 0.0


def test_rh_tax_summary_filters_other_years():
    trades = [
        {"ts": "2024-06-01T00:00:00+00:00", "ticker": "SPY", "dollar_pnl": 500.0, "is_win": True},
        {"ts": "2023-01-15T00:00:00+00:00", "ticker": "SPY", "dollar_pnl": 200.0, "is_win": True},
    ]
    with patch("app.rh_trade_record.get_all_trades", return_value=trades):
        from app.tax import compute_rh_tax_summary
        result = compute_rh_tax_summary(2025)

    assert result["total_trades"] == 0
    assert result["short_term_net"] == 0.0


# ── Alpaca FIFO matching ──────────────────────────────────────────────────────

def _make_order(symbol, side, qty, price, filled_at):
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.filled_qty = str(qty)
    o.filled_avg_price = str(price)
    o.filled_at = filled_at
    return o


def test_fifo_simple_short_term_gain():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 10, 500.0, datetime(2025, 1, 5, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 10, 550.0, datetime(2025, 6, 10, tzinfo=timezone.utc))

    events = _fifo_match([buy, sell], tax_year=2025)

    assert len(events) == 1
    assert events[0]["short_term"] is True
    assert abs(events[0]["gain"] - 500.0) < 0.01


def test_fifo_simple_long_term_gain():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 5, 400.0, datetime(2024, 1, 1, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 5, 500.0, datetime(2025, 3, 1, tzinfo=timezone.utc))

    events = _fifo_match([buy, sell], tax_year=2025)

    assert len(events) == 1
    assert events[0]["short_term"] is False
    assert abs(events[0]["gain"] - 500.0) < 0.01


def test_fifo_excludes_sells_outside_tax_year():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 10, 500.0, datetime(2024, 1, 5, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 10, 550.0, datetime(2024, 8, 10, tzinfo=timezone.utc))  # 2024

    events = _fifo_match([buy, sell], tax_year=2025)

    assert len(events) == 0


def test_fifo_partial_sell():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 10, 500.0, datetime(2025, 1, 5, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 4, 600.0, datetime(2025, 4, 5, tzinfo=timezone.utc))

    events = _fifo_match([buy, sell], tax_year=2025)

    assert len(events) == 1
    assert abs(events[0]["qty"] - 4.0) < 0.001
    assert abs(events[0]["gain"] - 400.0) < 0.01


def test_fifo_loss():
    from app.tax import _fifo_match

    buy = _make_order("SPY", "buy", 10, 600.0, datetime(2025, 2, 1, tzinfo=timezone.utc))
    sell = _make_order("SPY", "sell", 10, 550.0, datetime(2025, 4, 1, tzinfo=timezone.utc))

    events = _fifo_match([buy, sell], tax_year=2025)

    assert events[0]["gain"] < 0
    assert abs(events[0]["gain"] - (-500.0)) < 0.01


def test_fifo_unknown_basis_when_no_buy_lot():
    from app.tax import _fifo_match

    # Sell with no prior buy (position opened before fetch window)
    sell = _make_order("SPY", "sell", 5, 600.0, datetime(2025, 3, 1, tzinfo=timezone.utc))

    events = _fifo_match([sell], tax_year=2025)

    assert len(events) == 1
    assert events[0].get("unknown_basis") is True
    assert events[0]["gain"] is None


# ── Alpaca tax report — investor breakdown ────────────────────────────────────

@pytest.mark.asyncio
@patch("app.notifications.notify_alpaca_tax", new_callable=AsyncMock)
@patch("app.investors.load_investors")
@patch("app.tax.compute_alpaca_tax_summary")
async def test_alpaca_tax_investor_breakdown_uses_time_weighted_share(
    mock_summary, mock_load_investors, mock_notify
):
    """An investor who joined mid-year should be allocated a smaller share of the
    year's net gains than a full-year investor with the same deposit amount,
    proportional to their time-weighted capital."""
    from app.investors import Investor, Deposit, compute_time_weighted_capital
    from app.tax import send_alpaca_tax_report, _d

    mock_summary.return_value = {
        "short_term_gains": 1000.0,
        "short_term_losses": -200.0,
        "short_term_net": 800.0,
        "long_term_gains": 0.0,
        "long_term_losses": 0.0,
        "long_term_net": 0.0,
        "unknown_basis_proceeds": 0.0,
        "unknown_basis_count": 0,
        "sell_event_count": 5,
    }
    early = Investor(name="EarlyBird", deposits=[
        Deposit(amount=1000.0, entry_spy=600.0, date="2025-01-01"),
    ])
    mid = Investor(name="MidYear", deposits=[
        Deposit(amount=1000.0, entry_spy=650.0, date="2026-07-02"),
    ])
    mock_load_investors.return_value = [early, mid]

    await send_alpaca_tax_report(2026)

    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "EarlyBird" in msg and "MidYear" in msg

    early_cap = compute_time_weighted_capital(early, 2026)
    mid_cap = compute_time_weighted_capital(mid, 2026)
    total_cap = early_cap + mid_cap
    early_share = early_cap / total_cap
    mid_share = mid_cap / total_cap

    # MidYear joined partway through the year, so should get a smaller share
    # than EarlyBird despite depositing the same amount.
    assert mid_share < early_share
    assert f"({early_share * 100:.1f}%)" in msg
    assert f"({mid_share * 100:.1f}%)" in msg
    assert _d(800.0 * early_share) in msg
    assert _d(800.0 * mid_share) in msg


@pytest.mark.asyncio
@patch("app.notifications.notify_alpaca_tax", new_callable=AsyncMock)
@patch("app.investors.load_investors")
@patch("app.tax.compute_alpaca_tax_summary")
async def test_alpaca_tax_investor_breakdown_credits_departed_investor(
    mock_summary, mock_load_investors, mock_notify
):
    """An investor who withdrew everything mid-year still gets a non-zero share of the
    year's gains/losses, reflecting the capital they had at stake before withdrawing."""
    from app.investors import Investor, Deposit, compute_time_weighted_capital
    from app.tax import send_alpaca_tax_report

    mock_summary.return_value = {
        "short_term_gains": 1000.0,
        "short_term_losses": -200.0,
        "short_term_net": 800.0,
        "long_term_gains": 0.0,
        "long_term_losses": 0.0,
        "long_term_net": 0.0,
        "unknown_basis_proceeds": 0.0,
        "unknown_basis_count": 0,
        "sell_event_count": 5,
    }
    still_in = Investor(name="StillIn", deposits=[
        Deposit(amount=1000.0, entry_spy=600.0, date="2025-01-01"),
    ])
    departed = Investor(name="Departed", deposits=[
        Deposit(amount=1000.0, entry_spy=600.0, date="2025-01-01"),
        Deposit(amount=-1000.0, entry_spy=650.0, date="2026-07-02"),
    ])
    mock_load_investors.return_value = [still_in, departed]

    await send_alpaca_tax_report(2026)

    msg = mock_notify.call_args[0][0]
    departed_cap = compute_time_weighted_capital(departed, 2026)
    still_in_cap = compute_time_weighted_capital(still_in, 2026)
    assert departed_cap > 0
    share = departed_cap / (departed_cap + still_in_cap)
    assert "Departed" in msg
    assert f"({share * 100:.1f}%)" in msg


# ── /healthz endpoint ─────────────────────────────────────────────────────────

def test_healthz_alpaca_up():
    os.environ.setdefault("DISCORD_APP_ID", "test-app-id")
    from fastapi.testclient import TestClient
    with patch("app.main.get_account", return_value=MagicMock()):
        from app.main import app
        client = TestClient(app)
        resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["alpaca"] == "up"
    assert "robinhood" in data
    assert "timestamp" in data


def test_healthz_alpaca_down():
    os.environ.setdefault("DISCORD_APP_ID", "test-app-id")
    from fastapi.testclient import TestClient
    with patch("app.main.get_account", side_effect=Exception("timeout")):
        from app.main import app
        client = TestClient(app)
        resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["alpaca"] == "down"
    assert "timeout" in data["alpaca_error"]
