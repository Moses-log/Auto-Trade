"""End-to-end harness for run_monthly_rebalance.

run_monthly_rebalance is a large, tightly-coupled coroutine that fetches
positions, enriches them, calls Claude, parses a trade block, runs risk
guardrails, executes SELL -> TRIM -> BUY/DOUBLE_DOWN, and posts to Discord.
This module stubs every external seam (Robinhood, Claude, yfinance, macro,
history, Discord notifications, portfolio bookkeeping, logging) so the real
control flow and money math can be exercised deterministically and offline.

The `_rebalance_mocks()` context manager applies all patches and yields a
namespace of the mocks tests care about; individual tests override only what
they need (positions, buying power, broker results, the parsed trade block).
"""
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
# Do NOT add RH_USERNAME/RH_PASSWORD here — rh_client is fully mocked below and
# settings.rh_username/rh_password default to None. See tests/test_config_rh.py.

import contextlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _pos(symbol, qty, current_price, avg_entry_price=None):
    avg = avg_entry_price if avg_entry_price is not None else current_price * 0.8
    return {
        "symbol": symbol, "qty": qty, "avg_entry_price": avg,
        "current_price": current_price,
        "unrealized_pl": (current_price - avg) * qty,
        "unrealized_plpc": (current_price / avg - 1) * 100 if avg else 0.0,
    }


@contextlib.contextmanager
def _rebalance_mocks():
    """Patch every external dependency of run_monthly_rebalance and yield a
    SimpleNamespace of the mocks tests configure/assert on."""
    with contextlib.ExitStack() as stack:
        def p(target, **kw):
            return stack.enter_context(patch(target, **kw))

        rh = MagicMock()
        rh.available = True
        rh.get_all_positions_async = AsyncMock(return_value=[])
        rh.get_buying_power_async = AsyncMock(return_value=0.0)
        rh.close_ticker_async = AsyncMock()
        rh.sell_shares_async = AsyncMock()
        rh.buy_dollars_async = AsyncMock()
        p("app.trading.robinhood_client.rh_client", new=rh)

        # Data fetches (module-level in claude_manager) — no network.
        p("app.claude_manager._fetch_yf_data", side_effect=lambda sym: {"ticker": sym})
        p("app.claude_manager._fetch_technical_data", return_value={})
        p("app.claude_manager._fetch_spy_price", return_value=550.0)
        p("app.macro_context.fetch_macro_context", new=AsyncMock(return_value="Macro Context: calm"))
        p("app.claude_manager._load_recent_history", return_value=([], "No prior rebalance history on record."))
        p("app.claude_manager.build_live_scorecard", return_value=SimpleNamespace(outcomes=[]))
        p("app.claude_manager.resolve_sectors", return_value=({}, []))
        p("app.financials_chart.fetch_quarterly_financials", return_value=None)

        # Claude call + parse.
        call = p("app.claude_manager._call_claude_sync", return_value="Analysis. ```json\n{}\n```")
        parse = p("app.claude_manager._parse_trade_block", return_value={"no_changes": True, "trades": []})

        # Discord notifications (source module — imported locally in the coroutine).
        notify_embed = p("app.notifications.notify_claude_manager_embed", new=AsyncMock())
        notify_manager = p("app.notifications.notify_claude_manager", new=AsyncMock())
        notify_signal = p("app.notifications.notify_claude_signal_feed", new=AsyncMock())
        p("app.notifications.notify_claude_manager_with_chart", new=AsyncMock())
        p("app.notifications.notify_claude_signal_feed_with_chart", new=AsyncMock())

        # Portfolio bookkeeping + trade ledger.
        open_position = p("app.claude_portfolio.open_position", new=MagicMock())
        close_position = p("app.claude_portfolio.close_position", return_value=(0.0, 0.0, 0.0))
        trim_position = p("app.claude_portfolio.trim_position", return_value=(0.0, 0.0, 0.0))
        get_record = p("app.claude_portfolio.get_record", return_value=(0, 0))
        record_rh_trade = p("app.rh_trade_record.record_rh_trade", new=AsyncMock())

        # Queued after-hours sells schedule a pending-fill job — stub the
        # scheduler, pending-order store, and next-trading-day lookup.
        scheduler = p("app.scheduler.scheduler", new=MagicMock())
        save_pending = p("app.pending_orders.save_pending_order", new=MagicMock())
        p("app.trading.alpaca_client.get_next_trading_day", return_value=date(2026, 8, 3))

        # Logging, benchmark, background-task firing, sleeps.
        log = p("app.claude_manager._append_rebalance_log", new=MagicMock())
        p("app.claude_manager._format_benchmark", return_value="")
        p("app.claude_manager._fire", side_effect=lambda coro: coro.close())
        p("asyncio.sleep", new=AsyncMock())

        yield SimpleNamespace(
            rh=rh, call=call, parse=parse, log=log,
            notify_embed=notify_embed, notify_manager=notify_manager, notify_signal=notify_signal,
            open_position=open_position, close_position=close_position,
            trim_position=trim_position, get_record=get_record, record_rh_trade=record_rh_trade,
            scheduler=scheduler, save_pending=save_pending,
        )


def _logged(m):
    """The log_entry passed to _append_rebalance_log (always called in finally)."""
    assert m.log.call_count == 1
    return m.log.call_args[0][0]


@pytest.mark.asyncio
async def test_no_changes_completes_without_trading():
    with _rebalance_mocks() as m:
        m.rh.get_all_positions_async.return_value = [_pos("NVDA", 10.0, 450.0)]
        m.rh.get_buying_power_async.return_value = 1000.0
        m.parse.return_value = {"no_changes": True, "trades": []}

        from app.claude_manager import run_monthly_rebalance
        await run_monthly_rebalance()

    m.rh.buy_dollars_async.assert_not_called()
    m.rh.close_ticker_async.assert_not_called()
    assert _logged(m)["status"] == "no_changes"


@pytest.mark.asyncio
async def test_sell_then_double_down_executes_end_to_end():
    with _rebalance_mocks() as m:
        m.rh.get_all_positions_async.return_value = [
            _pos("NVDA", 2.0, 450.0),   # value 900
            _pos("MSFT", 10.0, 300.0),  # value 3000
        ]
        m.rh.get_buying_power_async.return_value = 2000.0
        m.rh.close_ticker_async.return_value = {
            "status": "ok", "qty": 10.0, "fill_price": 300.0, "queued": False,
        }
        m.close_position.return_value = (10.0, 200.0, 7.1)
        m.rh.buy_dollars_async.return_value = {"status": "ok", "qty": 1.27, "fill_price": 450.0}
        m.parse.return_value = {
            "no_changes": False,
            "trades": [
                {"action": "SELL", "ticker": "MSFT"},
                {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25},
            ],
        }

        from app.claude_manager import run_monthly_rebalance
        await run_monthly_rebalance()

    # portfolio = 900 + 3000 + 2000 = 5900; NVDA target 25% = 1475, delta = 575.
    # Budget = 2000 cash + 3000 realized MSFT proceeds = 5000; cap 4750 >= 575.
    m.rh.close_ticker_async.assert_awaited_once_with("MSFT")
    m.rh.buy_dollars_async.assert_awaited_once_with("NVDA", 575.0)
    logged = _logged(m)
    assert logged["status"] == "completed"
    actions = {t["action"] for t in logged["trades_executed"]}
    assert actions == {"SELL", "DOUBLE_DOWN"}


@pytest.mark.asyncio
async def test_skipped_sell_does_not_inflate_double_down_budget():
    """Rebalance-level guard for the phantom-proceeds fix: a SELL that skips
    must not fund a same-run DOUBLE_DOWN — the buy is sized against real cash."""
    with _rebalance_mocks() as m:
        m.rh.get_all_positions_async.return_value = [
            _pos("NVDA", 1.0, 100.0),   # value 100
            _pos("ARM", 10.0, 200.0),   # value 2000
        ]
        m.rh.get_buying_power_async.return_value = 500.0
        # ARM SELL skips — no proceeds materialize.
        m.rh.close_ticker_async.return_value = {"status": "error", "note": "no open position to close"}
        m.rh.buy_dollars_async.return_value = {"status": "ok", "qty": 4.75, "fill_price": 100.0}
        m.parse.return_value = {
            "no_changes": False,
            "trades": [
                {"action": "SELL", "ticker": "ARM"},
                {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25},
            ],
        }

        from app.claude_manager import run_monthly_rebalance
        await run_monthly_rebalance()

    # portfolio = 100 + 2000 + 500 = 2600; NVDA target 25% = 650, delta = 550.
    # Real budget is 500 only (ARM sell skipped); cap 500*0.95 = 475 — NOT 550.
    m.rh.buy_dollars_async.assert_awaited_once_with("NVDA", 475.0)
    logged = _logged(m)
    assert any(t["action"] == "SELL" and t["ticker"] == "ARM" for t in logged["trades_skipped"])


@pytest.mark.asyncio
async def test_trim_proceeds_fund_double_down():
    """A TRIM that executes must credit its proceeds so a same-run DOUBLE_DOWN
    can be funded by them — cash alone here ($100) is far too little."""
    with _rebalance_mocks() as m:
        m.rh.get_all_positions_async.return_value = [
            _pos("MSFT", 20.0, 300.0),  # value 6000 — the position we trim
            _pos("NVDA", 1.0, 100.0),   # value 100 — the position we add to
        ]
        m.rh.get_buying_power_async.return_value = 100.0
        m.rh.sell_shares_async.return_value = {"status": "ok", "qty": 15.0, "fill_price": 300.0}
        m.trim_position.return_value = (15.0, 500.0, 5.0)
        m.rh.buy_dollars_async.return_value = {"status": "ok", "qty": 14.5, "fill_price": 100.0}
        m.parse.return_value = {
            "no_changes": False,
            "trades": [
                {"action": "TRIM", "ticker": "MSFT", "target_weight_pct": 25},
                {"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25},
            ],
        }

        from app.claude_manager import run_monthly_rebalance
        await run_monthly_rebalance()

    # portfolio = 6000 + 100 + 100 = 6200; NVDA target 25% = 1550, delta = 1450.
    # Budget = 100 cash + (15 * $300 = 4500) TRIM proceeds = 4600; cap 4370 >= 1450,
    # so the full 1450 delta is invested — funded by the trim, not the $100 cash.
    m.rh.sell_shares_async.assert_awaited_once()
    assert m.rh.sell_shares_async.await_args.args[0] == "MSFT"
    m.rh.buy_dollars_async.assert_awaited_once_with("NVDA", 1450.0)
    logged = _logged(m)
    assert logged["status"] == "completed"
    assert {t["action"] for t in logged["trades_executed"]} == {"TRIM", "DOUBLE_DOWN"}


@pytest.mark.asyncio
async def test_failed_buy_is_recorded_as_skipped_not_executed():
    """When the broker rejects a buy, it must be logged as skipped (with the
    broker's reason) and never recorded as executed or booked to the portfolio."""
    with _rebalance_mocks() as m:
        m.rh.get_all_positions_async.return_value = [_pos("NVDA", 1.0, 100.0)]  # value 100
        m.rh.get_buying_power_async.return_value = 5000.0
        m.rh.buy_dollars_async.return_value = {"status": "error", "reason": "insufficient buying power"}
        m.parse.return_value = {
            "no_changes": False,
            "trades": [{"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 25}],
        }

        from app.claude_manager import run_monthly_rebalance
        await run_monthly_rebalance()

    # portfolio = 100 + 5000 = 5100; NVDA target 25% = 1275, delta = 1175 (budget ample).
    m.rh.buy_dollars_async.assert_awaited_once_with("NVDA", 1175.0)
    m.open_position.assert_not_called()          # never booked to the portfolio
    logged = _logged(m)
    assert logged["status"] == "completed"
    assert logged["trades_executed"] == []
    assert any(
        t["action"] == "DOUBLE_DOWN" and t["ticker"] == "NVDA"
        and t["reason"] == "insufficient buying power"
        for t in logged["trades_skipped"]
    )


@pytest.mark.asyncio
async def test_queued_sell_schedules_pending_fill_and_defers_pnl():
    """A SELL that queues after-hours must schedule a pending-fill job and save
    a pending order (source=manager), and must NOT book P&L yet — the fill
    price isn't known until the order fills at market open."""
    with _rebalance_mocks() as m:
        m.rh.get_all_positions_async.return_value = [_pos("NOW", 5.0, 950.0, avg_entry_price=900.0)]
        m.rh.get_buying_power_async.return_value = 0.0
        m.rh.close_ticker_async.return_value = {
            "status": "ok", "qty": 5.0, "price_est": 960.0, "queued": True, "order_id": "ord-xyz",
        }
        m.close_position.return_value = (5.0, None, None)  # P&L unknown until fill
        m.parse.return_value = {
            "no_changes": False,
            "trades": [{"action": "SELL", "ticker": "NOW"}],
        }

        from app.claude_manager import run_monthly_rebalance
        await run_monthly_rebalance()

    m.scheduler.add_job.assert_called_once()
    m.save_pending.assert_called_once()
    assert m.save_pending.call_args.kwargs["broker"] == "claude_sell"
    assert m.save_pending.call_args.kwargs["source"] == "manager"
    m.record_rh_trade.assert_not_awaited()       # no P&L booked while queued
    logged = _logged(m)
    assert logged["trades_executed"][0]["queued"] is True


@pytest.mark.asyncio
async def test_over_target_weight_is_clamped_to_25pct_before_sizing():
    """A DOUBLE_DOWN asking for 40% must be clamped to the 25% guardrail before
    the buy is sized — the invested dollars and the logged weight reflect 25%."""
    with _rebalance_mocks() as m:
        m.rh.get_all_positions_async.return_value = [_pos("NVDA", 1.0, 100.0)]  # value 100
        m.rh.get_buying_power_async.return_value = 5000.0
        m.rh.buy_dollars_async.return_value = {"status": "ok", "qty": 11.75, "fill_price": 100.0}
        m.parse.return_value = {
            "no_changes": False,
            "trades": [{"action": "DOUBLE_DOWN", "ticker": "NVDA", "target_weight_pct": 40}],
        }

        from app.claude_manager import run_monthly_rebalance
        await run_monthly_rebalance()

    # portfolio = 100 + 5000 = 5100. Unclamped 40% would target 2040 (delta 1940);
    # clamped to 25% it targets 1275, delta = 1175 — so the buy fires at 1175, and
    # the executed record shows the clamped 25% weight.
    m.rh.buy_dollars_async.assert_awaited_once_with("NVDA", 1175.0)
    logged = _logged(m)
    dd = next(t for t in logged["trades_executed"] if t["action"] == "DOUBLE_DOWN")
    assert dd["target_weight_pct"] == 25
