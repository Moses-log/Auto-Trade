import os
from types import SimpleNamespace
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, patch
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def _order(symbol, side, intent, qty, price, oid):
    return SimpleNamespace(
        symbol=symbol, side=SimpleNamespace(value=side),
        position_intent=SimpleNamespace(value=intent),
        filled_qty=str(qty), filled_avg_price=str(price),
        id=oid, filled_at=datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc),
    )


def test_classify_table():
    from app.alpaca_hf_notifier import _classify
    assert _classify(_order("QCOM", "buy", "buy_to_open", 1, 1, "a")) == ("LONG", "OPEN")
    assert _classify(_order("QCOM", "sell", "sell_to_close", 1, 1, "b")) == ("LONG", "CLOSE")
    assert _classify(_order("CSCO", "sell", "sell_to_open", 1, 1, "c")) == ("SHORT", "OPEN")
    assert _classify(_order("CSCO", "buy", "buy_to_close", 1, 1, "d")) == ("SHORT", "CLOSE")


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    import app.alpaca_hf_record as rec
    monkeypatch.setattr(rec, "_STATE_FILE", tmp_path / "hf.json")
    yield


@pytest.mark.asyncio
async def test_poll_seeds_first_run_no_backfill():
    import app.alpaca_hf_notifier as nf
    orders = [_order("QCOM", "buy", "buy_to_open", 12, 164.37, "q1")]
    with patch.object(nf, "get_orders_filled_range", return_value=orders), \
         patch.object(nf, "notify_hf_trade", new=AsyncMock()) as post:
        await nf.poll_and_notify()  # first run: seeds last_seen, no notify
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_poll_skips_spy_and_notifies_open():
    import app.alpaca_hf_notifier as nf
    import app.alpaca_hf_record as rec
    from datetime import datetime, timezone, timedelta
    await rec.set_last_seen(datetime.now(timezone.utc) - timedelta(hours=1))
    orders = [
        _order("SPY", "buy", "buy_to_open", 1, 770, "spy1"),
        _order("QCOM", "buy", "buy_to_open", 12, 164.37, "q1"),
    ]
    with patch.object(nf, "get_orders_filled_range", return_value=orders), \
         patch.object(nf, "notify_hf_trade", new=AsyncMock()) as post:
        await nf.poll_and_notify()
    assert post.await_count == 1
    assert "QCOM" in post.await_args.args[0]


@pytest.mark.asyncio
async def test_poll_dedups_second_run():
    import app.alpaca_hf_notifier as nf
    import app.alpaca_hf_record as rec
    from datetime import datetime, timezone, timedelta
    await rec.set_last_seen(datetime.now(timezone.utc) - timedelta(hours=1))
    orders = [_order("QCOM", "buy", "buy_to_open", 12, 164.37, "q1")]
    with patch.object(nf, "get_orders_filled_range", return_value=orders), \
         patch.object(nf, "notify_hf_trade", new=AsyncMock()) as post:
        await nf.poll_and_notify()
        await nf.poll_and_notify()
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_close_triggers_investor_split():
    import app.alpaca_hf_notifier as nf
    import app.alpaca_hf_record as rec
    from datetime import datetime, timezone, timedelta
    await rec.set_last_seen(datetime.now(timezone.utc) - timedelta(hours=1))
    await rec.record_open("QCOM", "LONG", 12, 164.37, "t", "open1")
    orders = [_order("QCOM", "sell", "sell_to_close", 12, 164.84, "close1")]
    inv_result = [SimpleNamespace(name="Alice", portfolio_share=100.0)]
    breakdown = SimpleNamespace(investors=inv_result)
    with patch.object(nf, "get_orders_filled_range", return_value=orders), \
         patch.object(nf, "notify_hf_trade", new=AsyncMock()) as post, \
         patch.object(nf, "load_investors", return_value=["x"]), \
         patch.object(nf, "get_account", return_value=SimpleNamespace(equity="1000")), \
         patch.object(nf, "compute_breakdown", return_value=breakdown):
        await nf.poll_and_notify()
    msg = post.await_args.args[0]
    assert "Alice" in msg and "WIN" in msg


def test_close_long_loss_uses_red_not_green():
    from app.alpaca_hf_notifier import format_close, CT
    from datetime import datetime
    msg = format_close("QCOM", "LONG", 12, 150.0, -50.0, -2.5, False, [], datetime.now(CT))
    assert "\U0001F7E2" not in msg
    assert "\U0001F534" in msg
