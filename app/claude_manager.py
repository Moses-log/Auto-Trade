"""
claude_manager.py — Autonomous Claude portfolio manager.

On the 1st of each month at 9:35 AM ET, Claude scores every current holding
and the broader opportunity set, decides what to buy/sell/hold, and executes
the trades automatically on Robinhood.

Requires ANTHROPIC_API_KEY in environment. Uses httpx (already a dependency)
to call the Anthropic Messages API directly — no anthropic SDK needed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, date, time as dtime
from typing import Optional

import httpx
import pytz
import yfinance as yf

from app.config import settings

_CT = pytz.timezone("America/Chicago")
_LOG_PATH = os.getenv("CLAUDE_REBALANCE_LOG_PATH", "/data/claude_rebalance_log.json")


def _timestamp() -> str:
    now = datetime.now(_CT)
    hour = int(now.strftime("%I"))
    return f"🕐 {hour}:{now.strftime('%M %p')} {now.strftime('%Z')} — {now.strftime('%B')} {now.day}, {now.year}"

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an institutional-grade equity portfolio manager whose sole objective is to maximize long-term risk-adjusted returns and outperform the S&P 500 over rolling 3-, 5-, and 10-year periods.

Portfolio Constraints:
- Hold a maximum of 10 stocks.
- Hold a minimum of 5 stocks.
- No ETFs, mutual funds, options, futures, leverage, or short positions.
- Only publicly traded U.S. stocks with a market capitalization above $5 billion.
- Cash allocation is flexible — hold as much cash as conviction warrants. Never force deployment into mediocre opportunities just to be fully invested.
- Position sizes may range from 5% to 25%.
- Rebalance monthly, but only when a superior opportunity exists.

Stock Selection Framework — score every stock 0–100 using this weighted model:
  Quality (30%): ROIC, ROE, Gross Margin Trends, Operating Margin Trends, Debt-to-Equity, Interest Coverage, FCF Consistency
  Growth (25%): Revenue Growth, EPS Growth, FCF Growth, TAM Expansion, Market Share Gains
  Momentum (20%): Relative Strength vs S&P 500, 6-Month Performance, 12-Month Performance, Above 200-Day MA, Institutional Accumulation
  Valuation (15%): Forward P/E, PEG, EV/EBITDA, FCF Yield, DCF Estimates
  Competitive Advantage (10%): Brand Strength, Network Effects, Switching Costs, Proprietary Technology, Industry Leadership

Portfolio Construction Rules:
- Prefer companies with durable competitive advantages.
- Prefer founder-led or highly aligned management teams.
- Avoid companies with deteriorating fundamentals.
- Avoid excessive debt.
- Avoid speculative meme stocks.
- Avoid companies with negative free cash flow unless growth is exceptional and clearly justified.
- Diversify across industries when possible.
- Do not allow any single sector to exceed 40% of the portfolio.
- Think like Bill Ackman and Leopold Aschenbrenner. Avoid mega-caps where possible. Maximize returns.
- If only 5–7 stocks meet the required standards, do not force diversification. Hold only the highest-conviction opportunities.
- Pay attention to earnings dates: avoid initiating or significantly increasing positions within 3 days of an earnings report unless you have very high conviction.
- Use macro context (VIX, 10Y yield, CPI) to calibrate overall risk appetite. High VIX favors defensiveness; rising yields pressure growth multiples.

POSITION SIZING ACTIONS:
- BUY: Open a new position or add to an existing one. The system uses delta-buy logic — it only invests the additional dollars needed to reach your target weight, not the full amount.
- DOUBLE_DOWN: Explicitly increase conviction in an existing position beyond its current weight. Executes identically to BUY (same delta-buy logic). Use when you want to signal elevated conviction.
- SELL: Close an entire position.
- TRIM: Reduce a position to a lower target weight without closing it. The system will sell the shares needed to reach your target. CRITICAL CONSTRAINT: TRIM is only possible if the position holds 1 or more whole shares. If qty < 1 (fractional position), you MUST use SELL to close it entirely — Robinhood does not support partial sells on sub-1-share positions.
- HOLD: Keep position at target weight; no trade executed.

HARD EXCLUSION — never buy, sell, trim, or mention as a candidate:
- SPY (managed separately by the Kimi DCA strategy — do not touch under any circumstances)

REQUIRED OUTPUT FORMAT:
After your full analysis, you MUST end your response with a JSON block in exactly this format:

```json
{
  "no_changes": false,
  "trades": [
    {"action": "BUY", "ticker": "FICO", "target_weight_pct": 15},
    {"action": "DOUBLE_DOWN", "ticker": "META", "target_weight_pct": 22},
    {"action": "SELL", "ticker": "NOW"},
    {"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 8},
    {"action": "HOLD", "ticker": "MSFT", "target_weight_pct": 20}
  ]
}
```

Rules for the JSON block:
- Set "no_changes": true if the portfolio requires no changes this month.
- action must be exactly "BUY", "DOUBLE_DOWN", "SELL", "TRIM", or "HOLD".
- ticker must be the exact US exchange ticker symbol.
- target_weight_pct is required for BUY, DOUBLE_DOWN, TRIM, and HOLD; omit for SELL.
- For TRIM: target_weight_pct is the weight you want to REDUCE TO (must be less than current weight).
- All target_weight_pct values for non-SELL positions should reflect true conviction — they do not need to sum to 100%. Uninvested cash is a valid position.
- Do not include markdown, comments, or extra fields in the JSON block."""


def _fetch_yf_data(ticker: str) -> dict:
    """Fetch key financial metrics for a single ticker via yfinance."""
    try:
        t = yf.Ticker(ticker)
        info = t.info

        # Upcoming earnings date
        days_to_earnings: Optional[int] = None
        try:
            cal = t.calendar
            dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
            today = date.today()
            for d in dates:
                d_date = d.date() if hasattr(d, "date") else d
                diff = (d_date - today).days
                if diff >= -1:
                    days_to_earnings = max(0, diff)
                    break
        except Exception:
            pass

        return {
            "ticker": ticker,
            "name": info.get("longName"),
            "sector": info.get("sector"),
            "market_cap_B": round(info.get("marketCap", 0) / 1e9, 1),
            "current_price": info.get("currentPrice"),
            "forward_pe": info.get("forwardPE"),
            "peg_ratio": info.get("pegRatio"),
            "ev_ebitda": info.get("enterpriseToEbitda"),
            "roe": info.get("returnOnEquity"),
            "profit_margin": info.get("profitMargins"),
            "gross_margin": info.get("grossMargins"),
            "operating_margin": info.get("operatingMargins"),
            "debt_to_equity": info.get("debtToEquity"),
            "revenue_growth_yoy": info.get("revenueGrowth"),
            "earnings_growth_yoy": info.get("earningsGrowth"),
            "52w_change": info.get("52WeekChange"),
            "days_to_earnings": days_to_earnings,
        }
    except Exception as exc:
        log.warning("yfinance error for %s: %s", ticker, exc)
        return {"ticker": ticker}


def _call_claude_sync(user_message: str) -> str:
    """Synchronous Anthropic API call. Run via executor to avoid blocking."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-opus-4-8",
            "max_tokens": 4096,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]


def _append_rebalance_log(entry: dict) -> None:
    try:
        try:
            with open(_LOG_PATH) as f:
                records = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            records = []
        records.append(entry)
        if len(records) > 36:          # cap at 3 years of monthly logs
            records = records[-36:]
        with open(_LOG_PATH, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as exc:
        log.warning("Failed to write rebalance log: %s", exc)


def _parse_trade_block(text: str) -> Optional[dict]:
    """Extract and parse the JSON trade block from Claude's response."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        log.warning("No JSON trade block found in Claude's response")
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        log.warning("Failed to parse Claude trade JSON: %s", exc)
        return None


def _fetch_spy_price() -> Optional[float]:
    try:
        return round(float(yf.Ticker("SPY").fast_info["lastPrice"]), 2)
    except Exception:
        return None


def _load_recent_history() -> tuple[list, str]:
    """Returns (raw_records, formatted_string) of the last 3 rebalance log entries."""
    try:
        with open(_LOG_PATH) as f:
            records = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return [], "No prior rebalance history on record."

    if not records:
        return [], "No prior rebalance history on record."

    recent = records[-3:]
    lines = ["Prior Rebalance History (last 3 months):"]
    for entry in recent:
        ts = entry.get("timestamp", "unknown")[:10]
        status = entry.get("status", "?")
        pv = entry.get("portfolio_value")
        pv_str = f"${pv:,.0f}" if pv else "unknown"
        executed = entry.get("trades_executed", [])
        buys = sum(1 for t in executed if t.get("action") in ("BUY", "DOUBLE_DOWN"))
        sells = sum(1 for t in executed if t.get("action") in ("SELL", "TRIM"))
        trade_str = f"{buys} buys, {sells} sells/trims" if executed else "no trades"
        spy = entry.get("spy_price_at_rebalance")
        spy_str = f", SPY=${spy:.2f}" if spy else ""
        lines.append(f"  {ts}: {status}, portfolio={pv_str}{spy_str}, trades={trade_str}")

    return records, "\n".join(lines)


def _format_benchmark(log_entry: dict, all_records: list) -> str:
    """Build a month-over-month and inception-to-date benchmark comparison string."""
    curr_pv = log_entry.get("portfolio_value")
    curr_spy = log_entry.get("spy_price_at_rebalance")
    if not all_records or not curr_pv or not curr_spy:
        return ""

    lines = []

    # Month-over-month vs previous rebalance
    prev = all_records[-1]
    prev_pv = prev.get("portfolio_value")
    prev_spy = prev.get("spy_price_at_rebalance")
    if prev_pv and prev_spy:
        port_chg = (curr_pv - prev_pv) / prev_pv * 100
        spy_chg = (curr_spy - prev_spy) / prev_spy * 100
        alpha = port_chg - spy_chg
        emoji = "🟢" if alpha >= 0 else "🔴"
        lines.append(
            f"{emoji} **This month:** Portfolio {port_chg:+.2f}%  |  SPY {spy_chg:+.2f}%  |  Alpha {alpha:+.2f}%"
        )

    # Inception-to-date vs first logged rebalance
    first = all_records[0]
    first_pv = first.get("portfolio_value")
    first_spy = first.get("spy_price_at_rebalance")
    if first_pv and first_spy and len(all_records) > 1:
        port_itd = (curr_pv - first_pv) / first_pv * 100
        spy_itd = (curr_spy - first_spy) / first_spy * 100
        alpha_itd = port_itd - spy_itd
        emoji_itd = "🟢" if alpha_itd >= 0 else "🔴"
        first_date = first.get("timestamp", "")[:10]
        lines.append(
            f"{emoji_itd} **Since {first_date}:** Portfolio {port_itd:+.2f}%  |  SPY {spy_itd:+.2f}%  |  Alpha {alpha_itd:+.2f}%"
        )

    return "\n".join(lines)


async def notify_claude_pending_sell_fill(
    order_id: str,
    ticker: str,
    entry_price: float,
    qty: float,
    source: str,
) -> None:
    """
    Called at market open to resolve a queued Claude sell order.
    Polls for actual fill, records the real P&L in the tax file, and sends Discord.
    source: "manager" or "autopilot"
    """
    import asyncio
    import robin_stocks.robinhood as r
    from app.rh_trade_record import record_rh_trade
    from app.notifications import notify_claude_manager, notify_claude_portfolio
    from app.pending_orders import remove_pending_order

    notify_fn = notify_claude_manager if source == "manager" else notify_claude_portfolio
    loop = asyncio.get_running_loop()
    fill_price: Optional[float] = None

    for attempt in range(12):
        try:
            info = await loop.run_in_executor(None, r.get_stock_order_info, order_id)
            if info and info.get("state") == "filled" and info.get("average_price"):
                fill_price = float(info["average_price"])
                break
        except Exception as exc:
            log.warning("Polling Claude sell order %s attempt %d: %s", order_id, attempt, exc)
        if attempt < 11:
            await asyncio.sleep(10)

    remove_pending_order(order_id)

    if fill_price is None:
        log.warning("Claude queued sell %s (%s) still unfilled after 2 min at open", order_id, ticker)
        await notify_fn(
            f"⚠️ Queued Claude sell for **{ticker}** did not fill at open — check Robinhood"
        )
        return

    dollar_pnl = (fill_price - entry_price) * qty
    pct_pnl = (fill_price - entry_price) / entry_price * 100 if entry_price else 0.0
    await record_rh_trade(dollar_pnl >= 0, ticker, dollar_pnl)

    from app.claude_portfolio import get_record
    wins, losses = get_record()

    if dollar_pnl >= 0:
        pnl_str = f"P&L: +${dollar_pnl:,.2f} (+{pct_pnl:.2f}%) 🟢 WIN"
    else:
        pnl_str = f"P&L: -${abs(dollar_pnl):,.2f} (-{abs(pct_pnl):.2f}%) 🔴 LOSS"

    prefix = "🤖 " if source == "autopilot" else ""
    await notify_fn(
        f"✅ {prefix}**KIMI SELL FILLED — {ticker}**\n"
        f"Qty: {qty:g} shares @ ${fill_price:,.2f}\n"
        f"{pnl_str}\n"
        f"Kimi Record: {wins}W - {losses}L\n"
        f"{_timestamp()}"
    )


async def run_monthly_rebalance() -> None:
    """
    Main entry point for the autonomous monthly portfolio rebalance.

    Flow:
      1. Fetch current RH positions + buying power
      2. Enrich each holding with yfinance fundamentals
      3. Ask Claude to score and rebalance the portfolio
      4. Parse the structured trade list from Claude's response
      5. Execute sells first, then buys
      6. Post full analysis + individual trade confirmations to Discord
      7. Save full audit log entry to /data/claude_rebalance_log.json
    """
    import asyncio
    from app.trading.robinhood_client import rh_client
    from app.notifications import notify_claude_manager, notify_claude_manager_with_chart

    async def _post_financials_chart(tkr: str) -> None:
        """Fetch quarterly financials and post chart to manager + subscriber channels."""
        try:
            from app.financials_chart import fetch_quarterly_financials, generate_financials_chart
            from app.notifications import notify_claude_signal_feed_with_chart
            loop = asyncio.get_running_loop()
            fin_data = await loop.run_in_executor(None, fetch_quarterly_financials, tkr)
            if fin_data is None:
                return
            chart_bytes = await loop.run_in_executor(None, generate_financials_chart, fin_data)
            await notify_claude_manager_with_chart("", chart_bytes)
            await notify_claude_signal_feed_with_chart("", chart_bytes)
        except Exception as exc:
            log.warning("Financials chart failed for %s: %s", tkr, exc)

    if not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set — skipping monthly rebalance")
        await notify_claude_manager(
            "⚠️ **KIMI PORTFOLIO REBALANCE SKIPPED**\n"
            "ANTHROPIC_API_KEY is not configured in environment."
        )
        return

    if not rh_client.available:
        log.warning("RH session unavailable — skipping monthly rebalance")
        await notify_claude_manager(
            "⚠️ **KIMI PORTFOLIO REBALANCE SKIPPED**\n"
            "Robinhood session is offline."
        )
        return

    await notify_claude_manager(
        f"🤖 **KIMI PORTFOLIO MANAGER — MONTHLY REBALANCE**\n"
        f"Fetching portfolio and running analysis... {_timestamp()}"
    )

    loop = asyncio.get_running_loop()

    log_entry: dict = {
        "timestamp": datetime.now(_CT).isoformat(),
        "status": "started",
        "portfolio_value": None,
        "buying_power": None,
        "spy_price_at_rebalance": None,
        "positions_before": [],
        "claude_response": None,
        "trade_block": None,
        "trades_executed": [],
        "trades_skipped": [],
    }

    try:
        # ── 1. Fetch current positions and buying power ───────────────────────
        try:
            positions = await rh_client.get_all_positions_async()
            buying_power = await rh_client.get_buying_power_async() or 0.0
        except Exception as exc:
            log.error("Failed to fetch RH portfolio data: %s", exc)
            log_entry["status"] = "failed_fetch"
            await notify_claude_manager(f"❌ **REBALANCE FAILED** — could not fetch portfolio: {exc}")
            return

        holdings_value = sum(p["qty"] * p.get("current_price", 0) for p in positions)
        portfolio_value = holdings_value + buying_power

        if portfolio_value < 1:
            log_entry["status"] = "skipped_zero_value"
            await notify_claude_manager("⚠️ **REBALANCE SKIPPED** — portfolio value is zero")
            return

        # Guard: if positions came back empty but buying power is suspiciously low
        # (suggesting a silent fetch failure rather than a genuine all-cash portfolio),
        # abort rather than letting Claude redeploy cash into a portfolio it can't see.
        if not positions and buying_power < portfolio_value * 0.99:
            log_entry["status"] = "failed_fetch"
            await notify_claude_manager(
                "⚠️ **REBALANCE ABORTED** — positions returned empty but buying power is low.\n"
                "This likely means the Robinhood API failed silently. Rebalance skipped to avoid "
                "over-buying into a portfolio whose current holdings are invisible."
            )
            return

        # ── 2. Enrich holdings + fetch SPY, macro context, history in parallel ──
        from app.macro_context import fetch_macro_context

        yf_tasks = [loop.run_in_executor(None, _fetch_yf_data, pos["symbol"]) for pos in positions]
        spy_task = loop.run_in_executor(None, _fetch_spy_price)

        all_results = await asyncio.gather(*yf_tasks, spy_task, fetch_macro_context(), return_exceptions=True)
        yf_results = all_results[:len(positions)]
        spy_price = all_results[len(positions)] if not isinstance(all_results[len(positions)], Exception) else None
        macro_text = all_results[len(positions) + 1] if not isinstance(all_results[len(positions) + 1], Exception) else "Macro context unavailable."

        all_history_records, history_text = _load_recent_history()

        enriched = []
        for pos, yf_data in zip(positions, yf_results):
            if isinstance(yf_data, Exception):
                yf_data = {"ticker": pos["symbol"]}
            weight_pct = round(
                pos["qty"] * pos.get("current_price", 0) / portfolio_value * 100, 1
            )
            enriched.append({
                **yf_data,
                "qty": pos["qty"],
                "avg_entry_price": pos["avg_entry_price"],
                "current_price": pos.get("current_price"),
                "unrealized_pnl": round(pos.get("unrealized_pl", 0), 2),
                "unrealized_pnl_pct": round(pos.get("unrealized_plpc", 0), 2),
                "current_weight_pct": weight_pct,
            })

        log_entry["portfolio_value"] = portfolio_value
        log_entry["buying_power"] = buying_power
        log_entry["positions_before"] = enriched
        log_entry["spy_price_at_rebalance"] = spy_price

        # ── 3. Build prompt and call Claude ───────────────────────────────────
        prompt = (
            f"Today is the monthly portfolio rebalance date.\n\n"
            f"Total Portfolio Value: ${portfolio_value:,.2f}\n"
            f"Available Cash/Buying Power: ${buying_power:,.2f} "
            f"({buying_power / portfolio_value * 100:.1f}% of portfolio)\n\n"
            f"{macro_text}\n\n"
            f"{history_text}\n\n"
            f"Current Holdings:\n{json.dumps(enriched, indent=2)}\n\n"
            f"Please:\n"
            f"1. Score each current holding using the full framework.\n"
            f"2. Identify any superior opportunities not currently held.\n"
            f"3. Determine the optimal portfolio for the next month.\n"
            f"4. Provide your full analysis with scores, then end with the required JSON trade block."
        )

        try:
            log.info("Calling Claude Opus for monthly portfolio rebalance...")
            response_text = await loop.run_in_executor(None, _call_claude_sync, prompt)
        except Exception as exc:
            log.error("Claude API call failed: %s", exc)
            log_entry["status"] = "failed_claude_api"
            await notify_claude_manager(f"❌ **REBALANCE FAILED** — Anthropic API error: {exc}")
            return

        log_entry["claude_response"] = response_text

        # ── 4. Parse trade block ──────────────────────────────────────────────
        trade_block = _parse_trade_block(response_text)
        log_entry["trade_block"] = trade_block

        # Full analysis → main Discord + subscriber feed
        analysis_body = re.sub(r"```json.*?```", "", response_text, flags=re.DOTALL).strip()
        analysis_msg = f"📊 **KIMI MONTHLY PORTFOLIO ANALYSIS**\n\n{analysis_body}"
        await notify_claude_manager(analysis_msg)
        from app.notifications import notify_claude_signal_feed
        asyncio.create_task(notify_claude_signal_feed(analysis_msg))

        if trade_block is None:
            log_entry["status"] = "failed_parse"
            await notify_claude_manager(
                "⚠️ Could not parse trade instructions from Claude's response. "
                "No trades executed — review the analysis above manually."
            )
            return

        if trade_block.get("no_changes"):
            log_entry["status"] = "no_changes"
            no_changes_msg = "✅ **NO CHANGES THIS MONTH**\nKimi Portfolio Manager determined the current portfolio requires no rebalancing."
            benchmark_str = _format_benchmark(log_entry, all_history_records)
            if benchmark_str:
                no_changes_msg += f"\n\n{benchmark_str}"
            await notify_claude_manager(no_changes_msg)
            return

        _EXCLUDED = {"SPY"}  # managed by Kimi — Claude must never touch these

        trades = [t for t in trade_block.get("trades", []) if t.get("ticker", "").upper() not in _EXCLUDED]
        if not trades:
            log_entry["status"] = "no_trades"
            await notify_claude_manager("✅ No trades to execute this month.")
            return

        action_count = len([t for t in trades if t["action"] != "HOLD"])
        await notify_claude_manager(f"⚡ **EXECUTING {action_count} TRADE(S)** — {_timestamp()}")

        from app.claude_portfolio import open_position, close_position, trim_position, get_record
        from app.trading.alpaca_client import get_next_trading_day

        _ET_TZ = pytz.timezone("America/New_York")

        def _schedule_pending_claude_sell(order_id: str, ticker: str, entry_px: float, qty_sold: float) -> None:
            from app.pending_orders import save_pending_order
            from app.scheduler import scheduler
            next_day = get_next_trading_day()
            run_dt = _ET_TZ.localize(datetime.combine(next_day, dtime(9, 31)))
            scheduler.add_job(
                notify_claude_pending_sell_fill, "date", run_date=run_dt,
                args=[order_id, ticker, entry_px, qty_sold, "manager"],
                id=f"pending_{order_id}", replace_existing=True,
            )
            save_pending_order(
                order_id, ticker, "SELL", None, entry_px,
                run_dt.isoformat(), broker="claude_sell", qty=qty_sold, source="manager",
            )

        # Pre-calculate expected sell proceeds so buys can be funded even when
        # sells are queued after-hours. SELL = full position; TRIM = only the
        # portion being sold (full position value − target value).
        expected_sell_proceeds = 0.0
        for _t in trades:
            _action = _t["action"]
            _tk = _t["ticker"].upper()
            _pos = next((p for p in positions if p["symbol"] == _tk), None)
            if _pos is None:
                continue
            _pos_val = _pos["qty"] * _pos.get("current_price", 0)
            if _action == "SELL":
                expected_sell_proceeds += _pos_val
            elif _action == "TRIM":
                _target_val = portfolio_value * _t.get("target_weight_pct", 5) / 100
                expected_sell_proceeds += max(0.0, _pos_val - _target_val)

        # ── 5. Execute full sells first ───────────────────────────────────────
        for trade in (t for t in trades if t["action"] == "SELL"):
            ticker = trade["ticker"].upper()
            result = await rh_client.close_ticker_async(ticker)

            if result.get("note"):
                await notify_claude_manager(f"ℹ️ KIMI SELL {ticker}: {result['note']}")
                log_entry["trades_skipped"].append({"action": "SELL", "ticker": ticker, "reason": result["note"]})
                continue

            if result.get("status") != "ok" or not result.get("qty"):
                reason = result.get("reason", "unknown")
                await notify_claude_manager(f"❌ **KIMI SELL — {ticker}** FAILED: {reason}")
                log_entry["trades_skipped"].append({"action": "SELL", "ticker": ticker, "reason": reason})
                continue

            qty    = result["qty"]
            fill   = result.get("fill_price") or result.get("price_est")
            queued = result.get("queued", False)
            entry_px = next((p.get("avg_entry_price", 0.0) for p in positions if p["symbol"] == ticker), 0.0)

            sold_qty, dollar_pnl, pct_pnl = close_position(ticker, fill or 0.0)

            if queued and result.get("order_id"):
                _schedule_pending_claude_sell(result["order_id"], ticker, entry_px, qty)
            elif dollar_pnl is not None:
                from app.rh_trade_record import record_rh_trade
                await record_rh_trade(dollar_pnl >= 0, ticker, dollar_pnl)

            wins, losses = get_record()
            log_entry["trades_executed"].append({
                "action": "SELL", "ticker": ticker, "qty": qty,
                "fill_price": fill, "queued": queued,
                "dollar_pnl": dollar_pnl, "pct_pnl": pct_pnl,
            })

            if queued:
                lines = [f"⏳ **KIMI SELL — {ticker}** (queued for open)",
                         f"Qty: {qty:g} shares ≈ ${result.get('price_est', 0):,.2f}", _timestamp()]
            else:
                pnl_line = (f"P&L: +${dollar_pnl:,.2f} (+{pct_pnl:.2f}%) 🟢 WIN" if dollar_pnl >= 0
                            else f"P&L: -${abs(dollar_pnl):,.2f} (-{abs(pct_pnl):.2f}%) 🔴 LOSS") if dollar_pnl is not None else ""
                lines = [f"🔴 **KIMI SELL — {ticker}**", f"Qty: {qty:g} shares @ ${fill:,.2f}"]
                if pnl_line:
                    lines.append(pnl_line)
                lines += [f"Kimi Record: {wins}W - {losses}L", _timestamp()]
            await notify_claude_manager("\n".join(lines))
            if not queued:
                from app.notifications import notify_claude_signal_feed
                sub_sig = [f"🔴 **KIMI SELL — {ticker}**", f"@ ${fill:,.2f}"]
                if dollar_pnl is not None:
                    sub_sig.append("🟢 WIN" if dollar_pnl >= 0 else "🔴 LOSS")
                sub_sig.append(_timestamp())
                asyncio.create_task(notify_claude_signal_feed("\n".join(sub_sig)))

        # ── 5b. Execute TRIMs ─────────────────────────────────────────────────
        for trade in (t for t in trades if t["action"] == "TRIM"):
            ticker = trade["ticker"].upper()
            target_wt = trade.get("target_weight_pct", 5)
            target_value = portfolio_value * target_wt / 100

            pos = next((p for p in positions if p["symbol"] == ticker), None)
            if pos is None:
                await notify_claude_manager(f"⚠️ TRIM {ticker}: no open position found")
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": "no position"})
                continue

            current_qty   = pos["qty"]
            current_price = pos.get("current_price", 0)
            current_value = current_qty * current_price

            if current_qty < 1.0:
                await notify_claude_manager(
                    f"⚠️ **TRIM {ticker} SKIPPED** — {current_qty:.4f} shares (< 1 whole share)\n"
                    f"Robinhood cannot partially sell fractional positions. Use SELL to close entirely."
                )
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": f"fractional ({current_qty:.4f} shares)"})
                continue

            if target_value >= current_value * 0.95:
                await notify_claude_manager(f"⚠️ TRIM {ticker}: already at or below {target_wt}% target")
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": "already at target"})
                continue

            sell_qty = round((current_value - target_value) / current_price, 6) if current_price > 0 else 0.0
            if sell_qty <= 0:
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": "sell qty <= 0"})
                continue

            result = await rh_client.sell_shares_async(ticker, sell_qty)

            if result.get("status") != "ok":
                reason = result.get("reason", "unknown")
                await notify_claude_manager(f"❌ **KIMI TRIM — {ticker}** FAILED: {reason}")
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": reason})
                continue

            qty_sold = result.get("qty", sell_qty)
            fill     = result.get("fill_price") or result.get("price_est")
            queued   = result.get("queued", False)
            entry_px = pos.get("avg_entry_price", 0.0)

            trimmed_qty, dollar_pnl, pct_pnl = trim_position(ticker, qty_sold, fill or 0.0)

            if queued and result.get("order_id"):
                _schedule_pending_claude_sell(result["order_id"], ticker, entry_px, qty_sold)
            elif dollar_pnl is not None:
                from app.rh_trade_record import record_rh_trade
                await record_rh_trade(dollar_pnl >= 0, ticker, dollar_pnl)

            wins, losses = get_record()
            log_entry["trades_executed"].append({
                "action": "TRIM", "ticker": ticker, "qty": qty_sold,
                "fill_price": fill, "queued": queued,
                "dollar_pnl": dollar_pnl, "target_weight_pct": target_wt,
            })

            if queued:
                lines = [f"⏳ **KIMI TRIM — {ticker}** (queued for open)",
                         f"Selling {qty_sold:g} shares ≈ ${result.get('price_est', 0):,.2f} → target {target_wt}%",
                         _timestamp()]
            else:
                pnl_line = (f"P&L: +${dollar_pnl:,.2f} 🟢" if dollar_pnl >= 0 else f"P&L: -${abs(dollar_pnl):,.2f} 🔴") if dollar_pnl is not None else ""
                lines = [f"✂️ **KIMI TRIM — {ticker}**",
                         f"Sold {qty_sold:g} shares @ ${fill:,.2f} → reduced to {target_wt}% target"]
                if pnl_line:
                    lines.append(pnl_line)
                lines += [f"Kimi Record: {wins}W - {losses}L", _timestamp()]
            await notify_claude_manager("\n".join(lines))
            if not queued:
                from app.notifications import notify_claude_signal_feed
                sub_sig = [f"✂️ **KIMI TRIM — {ticker}**", f"@ ${fill:,.2f} → target {target_wt}% weight", _timestamp()]
                asyncio.create_task(notify_claude_signal_feed("\n".join(sub_sig)))

        # Use pre-calculated sell proceeds + current cash as the buy budget.
        available_budget = buying_power + expected_sell_proceeds

        # Build a lookup of current position values for delta-buy calculation
        current_values = {pos["symbol"]: pos["qty"] * pos.get("current_price", 0) for pos in positions}

        # ── 6. Execute buys + double-downs ────────────────────────────────────
        for trade in (t for t in trades if t["action"] in ("BUY", "DOUBLE_DOWN")):
            ticker         = trade["ticker"].upper()
            action_label   = trade["action"]
            target_wt      = trade.get("target_weight_pct", 10)
            target_dollars = portfolio_value * target_wt / 100

            current_val    = current_values.get(ticker, 0.0)
            delta_dollars  = max(0.0, target_dollars - current_val)
            invest_dollars = min(delta_dollars, available_budget * 0.95)

            if invest_dollars < 1:
                if current_val >= target_dollars * 0.95:
                    reason = f"already at target ({current_val/portfolio_value*100:.1f}% vs {target_wt}% target)"
                else:
                    reason = f"needed ${delta_dollars:,.0f} more, only ${available_budget:,.0f} available"
                await notify_claude_manager(
                    f"⚠️ **KIMI {action_label} — {ticker}** skipped\n{reason}\n{_timestamp()}"
                )
                log_entry["trades_skipped"].append({"action": action_label, "ticker": ticker, "reason": reason})
                continue

            result = await rh_client.buy_dollars_async(ticker, invest_dollars)

            if result.get("status") != "ok":
                reason = result.get("reason", "unknown")
                await notify_claude_manager(f"❌ **KIMI {action_label} — {ticker}** FAILED: {reason}")
                log_entry["trades_skipped"].append({"action": action_label, "ticker": ticker, "reason": reason})
                continue

            qty    = result.get("qty", 0)
            fill   = result.get("fill_price")
            queued = result.get("queued", False)
            est    = result.get("price_est", 0)

            open_position(ticker, qty, fill or est or 0.0)
            log_entry["trades_executed"].append({
                "action": action_label, "ticker": ticker, "qty": qty,
                "fill_price": fill or est, "queued": queued,
                "dollars_invested": invest_dollars, "target_weight_pct": target_wt,
            })

            buy_emoji = "🔥" if action_label == "DOUBLE_DOWN" else "🟢"
            if queued:
                lines = [f"⏳ **KIMI {action_label} — {ticker}** (queued for open)",
                         f"Qty: {qty:g} shares ≈ ${est:,.2f}",
                         f"Target: {target_wt}% weight — Investing: ${invest_dollars:,.2f}", _timestamp()]
            else:
                lines = [f"{buy_emoji} **KIMI {action_label} — {ticker}**",
                         f"Qty: {qty:g} shares @ ${fill:,.2f}",
                         f"Target: {target_wt}% weight — Invested: ${invest_dollars:,.2f}", _timestamp()]

            await notify_claude_manager("\n".join(lines))
            if not queued:
                from app.notifications import notify_claude_signal_feed
                sig_emoji = "🔥" if action_label == "DOUBLE_DOWN" else "🟢"
                sub_sig = [
                    f"{sig_emoji} **KIMI {action_label} — {ticker}**",
                    f"@ ${fill:,.2f}",
                    f"Target: {target_wt}% weight",
                    _timestamp(),
                ]
                asyncio.create_task(notify_claude_signal_feed("\n".join(sub_sig)))
            asyncio.create_task(_post_financials_chart(ticker))
            available_budget = max(0.0, available_budget - invest_dollars)

        log_entry["status"] = "completed"

        # ── 7. Benchmark comparison ───────────────────────────────────────────
        benchmark_str = _format_benchmark(log_entry, all_history_records)
        completion_msg = f"✅ **KIMI PORTFOLIO REBALANCE COMPLETE** — {_timestamp()}"
        if benchmark_str:
            completion_msg += f"\n\n{benchmark_str}"
        await notify_claude_manager(completion_msg)

    finally:
        _append_rebalance_log(log_entry)
