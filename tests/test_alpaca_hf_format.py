import os
from datetime import datetime
import pytz
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
CT = pytz.timezone("America/Chicago")


def test_format_open_has_shares_and_notional():
    from app.alpaca_hf_notifier import format_open
    ts = CT.localize(datetime(2026, 8, 27, 15, 32))
    msg = format_open("QCOM", "LONG", 12, 164.77, ts)
    assert "OPEN" in msg and "LONG" in msg and "QCOM" in msg
    assert "12" in msg and "$164.77" in msg
    assert "$1,977.24" in msg


def test_format_close_win_shows_pnl_and_split():
    from app.alpaca_hf_notifier import format_close
    ts = CT.localize(datetime(2026, 8, 27, 10, 11))
    msg = format_close("QCOM", "LONG", 12, 164.84, 5.64, 0.29, True,
                       [("Alice", 3.10), ("Bob", 2.54)], ts)
    assert "WIN" in msg
    assert "+$5.64" in msg and "+0.29%" in msg
    assert "Alice" in msg and "+$3.10" in msg


def test_format_close_no_entry_is_na():
    from app.alpaca_hf_notifier import format_close
    ts = CT.localize(datetime(2026, 8, 27, 10, 11))
    msg = format_close("TSLA", "LONG", 5, 353.05, 0.0, 0.0, None, [], ts)
    assert "n/a" in msg


def test_format_recap_counts_and_winrate():
    from app.alpaca_hf_notifier import format_recap
    fills = [{"symbol": "QCOM", "role": "OPEN", "direction": "LONG",
              "qty": 12, "price": 164.37, "notional": 1972.44},
             {"symbol": "QCOM", "role": "CLOSE", "direction": "LONG",
              "qty": 12, "price": 164.84, "realized_pnl": 5.64}]
    msg = format_recap("August 27, 2026", fills, wins=5, losses=2, total_pnl=41.28)
    assert "5W" in msg and "2L" in msg
    assert "71%" in msg
    assert "+$41.28" in msg


def test_format_recap_closed_trade_not_listed_under_open():
    from app.alpaca_hf_notifier import format_recap
    fills = [{"symbol": "QCOM", "role": "OPEN", "direction": "LONG",
              "qty": 12, "price": 164.37, "notional": 1972.44},
             {"symbol": "QCOM", "role": "CLOSE", "direction": "LONG",
              "qty": 12, "price": 164.84, "realized_pnl": 5.64, "is_win": True}]
    msg = format_recap("August 27, 2026", fills, 1, 0, 5.64, open_lots=[])
    assert "Closed trades" in msg
    assert "Open trades" not in msg
    assert msg.count("QCOM") == 1


def test_format_recap_still_open_lot_listed_only_under_open():
    from app.alpaca_hf_notifier import format_recap
    fills = [{"symbol": "TSM", "role": "OPEN", "direction": "LONG",
              "qty": 4, "price": 425.47, "notional": 1701.88}]
    lots = [{"symbol": "TSM", "direction": "LONG", "qty": 4.0,
             "entry_price": 425.47, "entry_ts": "2026-08-27T15:00:00+00:00"},
            {"symbol": "PLTR", "direction": "SHORT", "qty": 2.0,
             "entry_price": 150.0, "entry_ts": "2026-08-20T15:00:00+00:00"}]
    msg = format_recap("August 27, 2026", fills, 0, 0, 0.0, open_lots=lots)
    assert "Open trades" in msg
    assert "Closed trades" not in msg
    assert "TSM" in msg and "PLTR" in msg  # carried-over lot included


def test_format_recap_no_fills_but_open_lot_still_shown():
    from app.alpaca_hf_notifier import format_recap
    lots = [{"symbol": "PLTR", "direction": "SHORT", "qty": 2.0,
             "entry_price": 150.0, "entry_ts": "x"}]
    msg = format_recap("August 27, 2026", [], 0, 0, 0.0, open_lots=lots)
    assert "PLTR" in msg and "Open trades" in msg
