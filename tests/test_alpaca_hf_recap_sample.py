"""End-to-end regression: drive the real FIFO record engine and format_recap
over the 2026-08-27 sample non-SPY fills and assert the aggregate wins/losses
and total realized P&L.

Round trips (all legs filled that day):
    QCOM  LONG  12  164.37 -> 164.84   pnl = (164.84-164.37)*12  = +5.6400  WIN
    CSCO  SHORT 17  112.18 -> 112.2469 pnl = (112.18-112.2469)*17 = -1.1373  LOSS
    TSM   LONG   4  425.47 -> 426.55   pnl = (426.55-425.47)*4   = +4.3200  WIN
    CRWD  SHORT  9  206.01 -> 205.76   pnl = (206.01-205.76)*9   = +2.2500  WIN
    MRNA  LONG  13  144.29 -> 143.39   pnl = (143.39-144.29)*13  = -11.7000 LOSS
    TSLA  LONG   5  352.9794 -> 353.05 pnl = (353.05-352.9794)*5 = +0.3530  WIN
Expected: 4 W / 2 L, total ~= -0.27.
Win/loss is counted by is_win (True/False), NOT by realized_pnl<=0.
"""
import os
import pytest

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    import app.alpaca_hf_record as rec
    monkeypatch.setattr(rec, "_STATE_FILE", tmp_path / "hf.json")
    yield


# (symbol, direction, qty, entry, exit)
_TRIPS = [
    ("QCOM", "LONG", 12, 164.37, 164.84),
    ("CSCO", "SHORT", 17, 112.18, 112.2469),
    ("TSM", "LONG", 4, 425.47, 426.55),
    ("CRWD", "SHORT", 9, 206.01, 205.76),
    ("MRNA", "LONG", 13, 144.29, 143.39),
    ("TSLA", "LONG", 5, 352.9794, 353.05),
]


def _expected_pnl(direction, qty, entry, exit_):
    return (exit_ - entry) * qty if direction == "LONG" else (entry - exit_) * qty


@pytest.mark.asyncio
async def test_sample_day_recap_totals():
    import app.alpaca_hf_record as rec
    from app.alpaca_hf_notifier import format_recap

    for sym, d, q, entry, exit_ in _TRIPS:
        await rec.record_open(sym, d, q, entry, "open_ts", f"{sym}-o")
        r = await rec.record_close(sym, d, q, exit_, "close_ts")
        await rec.record_daily_fill({
            "symbol": sym, "role": "OPEN", "direction": d,
            "qty": q, "price": entry, "notional": entry * q, "ts": "open_ts",
        })
        await rec.record_daily_fill({
            "symbol": sym, "role": "CLOSE", "direction": d,
            "qty": q, "price": exit_, "realized_pnl": r.realized_pnl,
            "is_win": r.is_win, "ts": "close_ts",
        })

    fills = await rec.pop_daily_fills()
    closes = [f for f in fills if f["role"] == "CLOSE"]
    wins = sum(1 for f in closes if f.get("is_win") is True)
    losses = sum(1 for f in closes if f.get("is_win") is False)
    total = sum(f["realized_pnl"] for f in closes)

    expected_total = round(
        sum(_expected_pnl(d, q, e, x) for _, d, q, e, x in _TRIPS), 2
    )

    assert wins == 4
    assert losses == 2
    assert round(total, 2) == expected_total
    assert expected_total == -0.27  # guards against a silent change in the sample math

    msg = format_recap("August 27, 2026", fills, wins, losses, total)
    assert "4 W" in msg
    assert "2 L" in msg
    assert "Fills today: 12" in msg  # 6 opens + 6 closes


@pytest.mark.asyncio
async def test_unmatched_close_not_counted_in_sample():
    """A close with no recorded open (is_win None) must not affect W/L."""
    import app.alpaca_hf_record as rec
    from app.alpaca_hf_notifier import format_recap

    r = await rec.record_close("ORPHAN", "LONG", 5, 100.0, "close_ts")
    assert r.is_win is None
    await rec.record_daily_fill({
        "symbol": "ORPHAN", "role": "CLOSE", "direction": "LONG",
        "qty": 5, "price": 100.0, "realized_pnl": r.realized_pnl,
        "is_win": r.is_win, "ts": "close_ts",
    })
    fills = await rec.pop_daily_fills()
    closes = [f for f in fills if f["role"] == "CLOSE"]
    wins = sum(1 for f in closes if f.get("is_win") is True)
    losses = sum(1 for f in closes if f.get("is_win") is False)
    assert wins == 0
    assert losses == 0
    msg = format_recap("August 27, 2026", fills, wins, losses, 0.0)
    assert "0 W" in msg and "0 L" in msg
