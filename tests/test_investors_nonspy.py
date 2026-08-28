import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def _make_investor(name, amount, entry_spy):
    from app.investors import Investor, Deposit
    return Investor(name=name, deposits=[Deposit(amount=amount, entry_spy=entry_spy, date="2026-01-01")], withdrawals=[])


def test_nonspy_contribution_split_by_share():
    from app.investors import compute_breakdown
    invs = [_make_investor("Alice", 5000, 100), _make_investor("Bob", 5000, 100)]
    b = compute_breakdown(invs, spy_price=100.0, real_total_equity=10000.0, nonspy_pnl=100.0)
    contribs = {r.name: r.nonspy_contribution for r in b.investors}
    assert round(contribs["Alice"], 2) == 50.0
    assert round(contribs["Bob"], 2) == 50.0


def test_nonspy_default_zero_preserves_behavior():
    from app.investors import compute_breakdown
    invs = [_make_investor("Alice", 5000, 100)]
    b = compute_breakdown(invs, spy_price=100.0, real_total_equity=5000.0)
    assert b.investors[0].nonspy_contribution == 0.0
