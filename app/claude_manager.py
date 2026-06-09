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
import re
from datetime import datetime
from typing import Optional

import httpx
import pytz
import yfinance as yf

from app.config import settings

_CT = pytz.timezone("America/Chicago")


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
- Cash allocation should remain below 10% unless market conditions are extremely unfavorable.
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

REQUIRED OUTPUT FORMAT:
After your full analysis, you MUST end your response with a JSON block in exactly this format:

```json
{
  "no_changes": false,
  "trades": [
    {"action": "BUY", "ticker": "FICO", "target_weight_pct": 15},
    {"action": "SELL", "ticker": "NOW"},
    {"action": "HOLD", "ticker": "MSFT", "target_weight_pct": 20}
  ]
}
```

Rules for the JSON block:
- Set "no_changes": true if the portfolio requires no changes this month.
- action must be exactly "BUY", "SELL", or "HOLD".
- ticker must be the exact US exchange ticker symbol.
- target_weight_pct is required for BUY and HOLD; omit for SELL.
- All target_weight_pct values for non-SELL positions must sum to 90–100%.
- Do not include markdown, comments, or extra fields in the JSON block."""


def _fetch_yf_data(ticker: str) -> dict:
    """Fetch key financial metrics for a single ticker via yfinance."""
    try:
        info = yf.Ticker(ticker).info
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
    """
    import asyncio
    from app.trading.robinhood_client import rh_client
    from app.notifications import notify_claude_manager

    if not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set — skipping monthly rebalance")
        await notify_claude_manager(
            "⚠️ **CLAUDE PORTFOLIO REBALANCE SKIPPED**\n"
            "ANTHROPIC_API_KEY is not configured in environment."
        )
        return

    if not rh_client.available:
        log.warning("RH session unavailable — skipping monthly rebalance")
        await notify_claude_manager(
            "⚠️ **CLAUDE PORTFOLIO REBALANCE SKIPPED**\n"
            "Robinhood session is offline."
        )
        return

    await notify_claude_manager(
        f"🤖 **CLAUDE PORTFOLIO MANAGER — MONTHLY REBALANCE**\n"
        f"Fetching portfolio and running analysis... {_timestamp()}"
    )

    loop = asyncio.get_running_loop()

    # ── 1. Fetch current positions and buying power ───────────────────────────
    try:
        positions = await rh_client.get_all_positions_async()
        buying_power = await rh_client.get_buying_power_async() or 0.0
    except Exception as exc:
        log.error("Failed to fetch RH portfolio data: %s", exc)
        await notify_claude_manager(f"❌ **REBALANCE FAILED** — could not fetch portfolio: {exc}")
        return

    holdings_value = sum(
        p["qty"] * p.get("current_price", 0) for p in positions
    )
    portfolio_value = holdings_value + buying_power

    if portfolio_value < 1:
        await notify_claude_manager("⚠️ **REBALANCE SKIPPED** — portfolio value is zero")
        return

    # ── 2. Enrich holdings with yfinance data ─────────────────────────────────
    enriched = []
    for pos in positions:
        ticker = pos["symbol"]
        yf_data = await loop.run_in_executor(None, _fetch_yf_data, ticker)
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

    # ── 3. Build prompt and call Claude ───────────────────────────────────────
    prompt = (
        f"Today is the monthly portfolio rebalance date.\n\n"
        f"Total Portfolio Value: ${portfolio_value:,.2f}\n"
        f"Available Cash/Buying Power: ${buying_power:,.2f} "
        f"({buying_power / portfolio_value * 100:.1f}% of portfolio)\n\n"
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
        await notify_claude_manager(f"❌ **REBALANCE FAILED** — Anthropic API error: {exc}")
        return

    # ── 4. Parse trade block ──────────────────────────────────────────────────
    trade_block = _parse_trade_block(response_text)

    # Full analysis → analysis channel
    analysis_body = re.sub(r"```json.*?```", "", response_text, flags=re.DOTALL).strip()
    await notify_claude_manager(f"📊 **CLAUDE MONTHLY PORTFOLIO ANALYSIS**\n\n{analysis_body}")

    if trade_block is None:
        await notify_claude_manager(
            "⚠️ Could not parse trade instructions from Claude's response. "
            "No trades executed — review the analysis above manually."
        )
        return

    if trade_block.get("no_changes"):
        await notify_claude_manager(
            "✅ **NO CHANGES THIS MONTH**\n"
            "Claude determined the current portfolio requires no rebalancing."
        )
        return

    trades = trade_block.get("trades", [])
    if not trades:
        await notify_claude_manager("✅ No trades to execute this month.")
        return

    action_count = len([t for t in trades if t["action"] != "HOLD"])
    await notify_claude_manager(
        f"⚡ **EXECUTING {action_count} TRADE(S)** — {_timestamp()}"
    )

    from app.claude_portfolio import open_position, close_position, get_record

    # ── 5. Execute sells first ────────────────────────────────────────────────
    for trade in (t for t in trades if t["action"] == "SELL"):
        ticker = trade["ticker"].upper()
        result = await rh_client.close_ticker_async(ticker)

        if result.get("note"):
            await notify_claude_manager(f"ℹ️ CLAUDE SELL {ticker}: {result['note']}")
            continue

        if result.get("status") != "ok" or not result.get("qty"):
            await notify_claude_manager(
                f"❌ **CLAUDE SELL — {ticker}** FAILED: {result.get('reason', 'unknown')}"
            )
            continue

        qty      = result["qty"]
        fill     = result.get("fill_price")
        queued   = result.get("queued", False)

        sold_qty, dollar_pnl, pct_pnl = close_position(ticker, fill or 0.0)
        wins, losses = get_record()
        record_str = f"{wins}W - {losses}L"

        if queued:
            lines = [
                f"⏳ **CLAUDE SELL — {ticker}** (queued for open)",
                f"Qty: {qty:g} shares ≈ ${result.get('price_est', 0):,.2f}",
                _timestamp(),
            ]
        else:
            pnl_line = ""
            if dollar_pnl is not None and pct_pnl is not None:
                if dollar_pnl >= 0:
                    pnl_line = f"P&L: +${dollar_pnl:,.2f} (+{pct_pnl:.2f}%) 🟢 WIN"
                else:
                    pnl_line = f"P&L: -${abs(dollar_pnl):,.2f} (-{abs(pct_pnl):.2f}%) 🔴 LOSS"
            lines = [
                f"🔴 **CLAUDE SELL — {ticker}**",
                f"Qty: {qty:g} shares @ ${fill:,.2f}",
            ]
            if pnl_line:
                lines.append(pnl_line)
            lines += [f"Claude Record: {record_str}", _timestamp()]

        await notify_claude_manager("\n".join(lines))

    # Refresh buying power after sells settle
    try:
        buying_power = await rh_client.get_buying_power_async() or buying_power
    except Exception:
        pass

    # ── 6. Execute buys ───────────────────────────────────────────────────────
    for trade in (t for t in trades if t["action"] == "BUY"):
        ticker        = trade["ticker"].upper()
        target_wt     = trade.get("target_weight_pct", 10)
        target_dollars = portfolio_value * target_wt / 100
        invest_dollars = min(target_dollars, buying_power * 0.95)

        if invest_dollars < 1:
            await notify_claude_manager(
                f"⚠️ **CLAUDE BUY — {ticker}** skipped\n"
                f"Needed ${target_dollars:,.0f}, only ${buying_power:,.0f} available\n"
                f"{_timestamp()}"
            )
            continue

        result = await rh_client.buy_dollars_async(ticker, invest_dollars)

        if result.get("status") != "ok":
            await notify_claude_manager(
                f"❌ **CLAUDE BUY — {ticker}** FAILED: {result.get('reason', 'unknown')}"
            )
            continue

        qty    = result.get("qty", 0)
        fill   = result.get("fill_price")
        queued = result.get("queued", False)
        est    = result.get("price_est", 0)

        open_position(ticker, qty, fill or est or 0.0)

        if queued:
            lines = [
                f"⏳ **CLAUDE BUY — {ticker}** (queued for open)",
                f"Qty: {qty:g} shares ≈ ${est:,.2f}",
                f"Target: {target_wt}% weight — Invested: ${invest_dollars:,.2f}",
                _timestamp(),
            ]
        else:
            lines = [
                f"🟢 **CLAUDE BUY — {ticker}**",
                f"Qty: {qty:g} shares @ ${fill:,.2f}",
                f"Target: {target_wt}% weight — Invested: ${invest_dollars:,.2f}",
                _timestamp(),
            ]

        await notify_claude_manager("\n".join(lines))
        buying_power = max(0.0, buying_power - invest_dollars)

    await notify_claude_manager(f"✅ **CLAUDE PORTFOLIO REBALANCE COMPLETE** — {_timestamp()}")
