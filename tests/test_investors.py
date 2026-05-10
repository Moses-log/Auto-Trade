import json
import pytest


def test_load_investors_returns_empty_list_when_file_missing(tmp_path):
    from app.investors import load_investors
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
