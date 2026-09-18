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


@pytest.mark.asyncio
async def test_long_roundtrip_win():
    import app.alpaca_hf_record as rec
    await rec.record_open("QCOM", "LONG", 12, 164.37, "t1", "o1")
    r = await rec.record_close("QCOM", "LONG", 12, 164.84, "t2")
    assert r.matched_qty == 12
    assert round(r.realized_pnl, 2) == 5.64
    assert r.is_win is True
    assert r.unmatched_qty == 0


@pytest.mark.asyncio
async def test_short_roundtrip_loss():
    import app.alpaca_hf_record as rec
    # sell_to_open @112.18, buy_to_close @112.2469 -> short loses
    await rec.record_open("CSCO", "SHORT", 17, 112.18, "t1", "o1")
    r = await rec.record_close("CSCO", "SHORT", 17, 112.2469, "t2")
    assert r.realized_pnl < 0
    assert r.is_win is False


@pytest.mark.asyncio
async def test_partial_close_leaves_lot():
    import app.alpaca_hf_record as rec
    await rec.record_open("AMZN", "LONG", 7, 256.80, "t1", "o1")
    r = await rec.record_close("AMZN", "LONG", 4, 257.40, "t2")
    assert r.matched_qty == 4
    assert r.unmatched_qty == 0
    r2 = await rec.record_close("AMZN", "LONG", 3, 257.40, "t3")
    assert r2.matched_qty == 3


@pytest.mark.asyncio
async def test_close_matches_newest_lot_lifo():
    """A close pairs with the most recently opened matching lot (LIFO), not the
    oldest. Regression for the PLTR phantom-loss bug: a stale older short lot
    must not be consumed by a close that belongs to a newer bracket."""
    import app.alpaca_hf_record as rec
    # Old, still-open short from a prior week @167.17 (must be left untouched).
    await rec.record_open("PLTR", "SHORT", 8, 167.1701, "t1", "old")
    # New same-second bracket: sell_to_open @174.82 ...
    await rec.record_open("PLTR", "SHORT", 5, 174.82, "t2", "new")
    # ... buy_to_close @174.41 -> this closes the NEW lot -> a win.
    r = await rec.record_close("PLTR", "SHORT", 5, 174.41, "t3")
    assert r.matched_qty == 5
    assert round(r.realized_pnl, 2) == 2.05   # (174.82 - 174.41) * 5
    assert r.is_win is True
    # Old lot survives intact, unchanged qty and price.
    lots = rec._load()["open_lots"]["PLTR"]
    assert len(lots) == 1
    assert lots[0]["qty"] == 8
    assert lots[0]["entry_price"] == 167.1701


@pytest.mark.asyncio
async def test_close_spills_newest_to_older_lifo():
    """A close larger than the newest lot spills into the next-newest, oldest last."""
    import app.alpaca_hf_record as rec
    await rec.record_open("NVDA", "SHORT", 4, 100.0, "t1", "a")  # oldest
    await rec.record_open("NVDA", "SHORT", 3, 110.0, "t2", "b")  # newest
    r = await rec.record_close("NVDA", "SHORT", 5, 105.0, "t3")
    # 3 @110 -> (110-105)*3=+15 ; 2 @100 -> (100-105)*2=-10 ; net +5
    assert r.matched_qty == 5
    assert round(r.realized_pnl, 2) == 5.0
    lots = rec._load()["open_lots"]["NVDA"]
    assert len(lots) == 1
    assert lots[0]["entry_price"] == 100.0
    assert lots[0]["qty"] == 2


@pytest.mark.asyncio
async def test_close_without_open_is_neutral():
    import app.alpaca_hf_record as rec
    r = await rec.record_close("TSLA", "LONG", 5, 353.05, "t1")
    assert r.matched_qty == 0
    assert r.is_win is None
    assert r.unmatched_qty == 5


@pytest.mark.asyncio
async def test_dedup_and_last_seen_persist():
    import app.alpaca_hf_record as rec
    from datetime import datetime, timezone
    assert await rec.is_seen("o1") is False
    await rec.mark_seen("o1")
    assert await rec.is_seen("o1") is True
    dt = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)
    await rec.set_last_seen(dt)
    assert await rec.get_last_seen() == dt


@pytest.mark.asyncio
async def test_daily_fills_buffer_pop_clears():
    import app.alpaca_hf_record as rec
    await rec.record_daily_fill({"symbol": "QCOM", "role": "OPEN"})
    fills = await rec.pop_daily_fills()
    assert len(fills) == 1
    assert await rec.pop_daily_fills() == []


@pytest.mark.asyncio
async def test_contribution_total_sums_closed():
    import app.alpaca_hf_record as rec
    await rec.record_open("QCOM", "LONG", 12, 164.37, "t1", "o1")
    await rec.record_close("QCOM", "LONG", 12, 164.84, "t2")
    assert round(await rec.contribution_total(), 2) == 5.64


@pytest.mark.asyncio
async def test_contribution_by_investor_freezes_shares_at_close():
    import app.alpaca_hf_record as rec
    # Trade closes with shares 60/40 -> pnl +100 -> Hoang 60, Moses 40 frozen.
    await rec.record_open("TSLA", "LONG", 5, 200.0, "t1", "o1")
    await rec.record_close("TSLA", "LONG", 5, 220.0, "t2",
                           shares=[("Hoang", 60.0), ("Moses", 40.0)])
    # Shares later shift to 50/50; frozen split must ignore the change.
    by = await rec.contribution_by_investor(fallback_shares=[("Hoang", 50.0), ("Moses", 50.0)])
    assert by == {"Hoang": 60.0, "Moses": 40.0}


@pytest.mark.asyncio
async def test_contribution_by_investor_legacy_falls_back_to_current():
    import app.alpaca_hf_record as rec
    # Legacy closed trade written without a stored split.
    st = rec._load()
    st["closed_trades"].append({
        "symbol": "X", "direction": "LONG", "qty": 1, "exit_price": 10.0,
        "realized_pnl": 50.0, "pct": 1.0, "is_win": True, "closed_ts": "t",
    })
    rec._save(st)
    by = await rec.contribution_by_investor(fallback_shares=[("Hoang", 60.0), ("Moses", 40.0)])
    assert by == {"Hoang": 30.0, "Moses": 20.0}
