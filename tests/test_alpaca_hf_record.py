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
