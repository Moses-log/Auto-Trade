"""
claude_manager.py — Autonomous Claude portfolio manager.

On the 1st of each month at 9:35 AM ET, Claude scores every current holding
and the broader opportunity set, decides what to buy/sell/hold, and executes
the trades automatically on Robinhood.

Requires ANTHROPIC_API_KEY in environment. Uses httpx (already a dependency)
to call the Anthropic Messages API directly — no anthropic SDK needed.
"""

from __future__ import annotations

import asyncio
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
from app.decision_review import build_live_scorecard, format_scorecard_embed
from app.risk_guardrails import (
    clamp_position_weights, resolve_sectors, _yf_sector_fetch,
    compute_sector_exposure, sector_warnings, format_guardrail_embed,
)

_CT = pytz.timezone("America/Chicago")
_LOG_PATH = os.getenv("CLAUDE_REBALANCE_LOG_PATH", "/data/claude_rebalance_log.json")

# Discord embed color palette (decimal integers)
_CLR_YELLOW = 0xF5E642   # accent / header / double-down
_CLR_GREEN  = 0x00C853   # buy / win / complete
_CLR_RED    = 0xFF4D4D   # sell / loss
_CLR_ORANGE = 0xFFA032   # trim
_CLR_GRAY   = 0x6B6B6B   # neutral / hold / info

# Keeps strong references to fire-and-forget tasks so GC cannot cancel them mid-execution.
_bg_tasks: set = set()


def _fire(coro) -> None:
    """Schedule a coroutine as a background task that won't be GC'd."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _timestamp() -> str:
    now = datetime.now(_CT)
    hour = int(now.strftime("%I"))
    return f"🕐 {hour}:{now.strftime('%M %p')} {now.strftime('%Z')} — {now.strftime('%B')} {now.day}, {now.year}"

log = logging.getLogger(__name__)


def _embed(
    title: str,
    color: int,
    *,
    description: str = "",
    fields: list[dict] | None = None,
    footer: str = "",
) -> dict:
    """Build a Discord embed payload dict."""
    e: dict = {"title": title[:256], "color": color}
    if description:
        e["description"] = description[:4096]
    if fields:
        e["fields"] = fields
    if footer:
        e["footer"] = {"text": footer[:2048]}
    return e


def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name[:256], "value": value[:1024], "inline": inline}


def _trade_embed(action: str, ticker: str, fields: list[dict], footer: str) -> dict:
    """Color-coded embed for a single trade."""
    colors = {
        "BUY": _CLR_GREEN, "DOUBLE_DOWN": _CLR_YELLOW,
        "SELL": _CLR_RED,  "TRIM": _CLR_ORANGE, "HOLD": _CLR_GRAY,
    }
    emojis = {
        "BUY": "🟢", "DOUBLE_DOWN": "🔥",
        "SELL": "🔴", "TRIM": "✂️",  "HOLD": "⏸",
    }
    label = "DOUBLE DOWN" if action == "DOUBLE_DOWN" else action
    return _embed(
        f"{emojis.get(action, '📌')} KIMI {label} — {ticker}",
        colors.get(action, _CLR_GRAY),
        fields=fields,
        footer=footer,
    )

_DIVIDER = "══════════════════════════════"


def _section_ticker(section: str) -> str:
    """Extract the ticker symbol from a '## TICKER — Name' header line."""
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("## "):
            first_word = line[3:].split("—")[0].split("–")[0].strip().split()[0]
            return first_word.upper()
    return ""

_SYSTEM_PROMPT = """You are an institutional-grade portfolio manager whose objective is to outperform the S&P 500 over rolling 3-, 5-, and 10-year periods by identifying the companies most likely to dominate the future economy.

========================
CORE PHILOSOPHY
========================

The stock market rewards companies that solve the world's biggest future problems.

Do not simply buy cheap companies.
Do not simply buy large companies.
Identify the businesses creating the future.

Prioritize innovation, technological disruption, and long-term economic transformation while maintaining strict standards for profitability, valuation, and execution.

Your goal is not to mirror the economy. Your goal is to identify the companies that will create the next economy.

========================
PORTFOLIO CONSTRAINTS
========================

- Maximum 10 stocks, minimum 5 stocks.
- No ETFs, mutual funds, options, futures, leverage, or short positions.
- U.S.-listed equities only, market cap above $2 billion.
- Cash allocation below 10% unless market conditions are exceptionally unfavorable.
- Maximum position size: 25%. Maximum single sector: 50%.
- Rebalance monthly only when superior opportunities exist.
- Pay attention to earnings dates: avoid initiating or significantly increasing positions within 3 days of an earnings report unless conviction is very high.
- Use macro context (VIX, 10Y yield, CPI) to calibrate overall risk appetite. High VIX favors defensiveness; rising yields pressure growth multiples.
- If a holding's JSON includes a "_data_gaps" field, those listed metrics were unavailable this run — weight your analysis toward the available data and note the limitation in your reasoning.

========================
FUTURE DOMINANCE THEMES
========================

Give additional weighting to companies operating within industries expected to experience major secular growth over the next decade. Current themes (evolve these as the world evolves):

ARTIFICIAL INTELLIGENCE: AI infrastructure, AI chips, AI software, agentic AI, data centers, AI cybersecurity, AI robotics
ROBOTICS & AUTOMATION: Industrial robotics, humanoid robotics, warehouse automation, autonomous systems
SPACE ECONOMY: Launch providers, satellite networks, space infrastructure, orbital communications, defense-space integration
ENERGY TRANSFORMATION: Nuclear energy, SMRs, energy storage, grid modernization, electrification, advanced power generation
BIOTECHNOLOGY: Gene editing, precision medicine, longevity technologies, drug discovery platforms
DIGITAL INFRASTRUCTURE: Cloud computing, cybersecurity, semiconductors, networking infrastructure
DEFENSE TECHNOLOGY: Autonomous defense systems, military AI, advanced aerospace, strategic defense systems

========================
ADAPTIVE FUTURE FRAMEWORK
========================

The world changes. At every portfolio review:
1. Identify the most important global trends.
2. Determine which technologies are becoming necessities.
3. Determine where governments, corporations, and consumers are allocating capital.
4. Identify emerging and declining industries.
5. Shift portfolio weighting toward future winners.

Never become anchored to old themes. Examples: AI dominates today. Quantum computing may dominate tomorrow. Fusion energy or space manufacturing may emerge later. Continuously update themes based on real-world developments.

========================
STOCK SCORING SYSTEM (0–100)
========================

Quality (20%): ROIC, ROE, Gross Margin Trends, Operating Margin Trends, Balance Sheet strength, Debt levels, FCF Consistency
Growth (25%): Revenue growth, EPS growth, FCF growth, Market expansion, TAM growth
Future Dominance Potential (25%): Exposure to future megatrends, ability to become a category leader, technological leadership, innovation velocity, R&D investment, patent portfolio, talent density, strategic positioning
Momentum (15%): Relative Strength vs S&P 500, Institutional accumulation, Price trend quality, Above 200-day MA
Valuation (15%): Forward P/E, PEG Ratio, EV/EBITDA, FCF Yield, Growth-adjusted valuation

========================
INNOVATION PREFERENCE RULES
========================

When two stocks have similar scores, prefer:
- Founder-led companies and visionary management teams
- Category creators and platform businesses
- Companies disrupting large industries

Avoid:
- Slow-growth mature businesses
- Commodity and legacy businesses with limited innovation
- Companies dependent solely on economic cycles

========================
CONVICTION MULTIPLIER
========================

Increase conviction when companies exhibit: explosive revenue growth, expanding margins, large addressable markets, technological moats, network effects, strong execution, rapid adoption curves. Favor businesses with the potential to become dominant players within their industries.

========================
MEGA-WINNER RULE
========================

The greatest stock market outperformance historically comes from a small number of extraordinary winners. Actively search for potential 10x opportunities while maintaining reasonable risk controls.

A company does not need to be profitable today if:
- Revenue growth exceeds 25% annually
- Gross margins are strong
- The addressable market is massive
- The path to profitability is credible
- The company is a leader in a future-dominant industry

Accept moderate short-term volatility in exchange for significantly higher long-term returns.

========================
BUBBLE PROTECTION RULES
========================

Do NOT buy a company solely because it is popular or trending. Require:
- Real revenue
- Improving fundamentals
- Strong balance sheet
- Evidence of execution
- Sustainable competitive advantages

Reject companies whose valuation is disconnected from realistic future cash flow potential.

========================
ULTIMATE OBJECTIVE
========================

Build a concentrated portfolio of exceptional businesses with the highest probability of significantly outperforming the S&P 500 over long periods. Only own companies with a credible path toward becoming dominant forces in the future global economy. The goal is not to find good companies — the goal is to find the future winners of the next decade.

========================
RESEARCH RIGOR REQUIREMENTS
========================

For every stock under consideration — whether a current holding or a new candidate — apply this structured research framework before sizing or recommending it.

SECTION 1 — FOUNDATION
- What is the exact business model? How does it make money? Core product in plain English.
- Moat and competition: top 3 competitors. Does the company have a unique technological advantage or patent competitors lack?
- Top 3 upcoming catalysts (product launches, regulatory approvals, partnerships) in the next 12 months. Rate each Critical, High, or Strategic.
- Asymmetry check: low valuation floor vs high growth ceiling?

SECTION 2 — VALUATION RIGOR
- Rule of 40: Revenue Growth % + EBITDA Margin %. Companies trending above 40 are quality growth businesses.
- Value/Growth Score: P/S TTM ÷ YoY Revenue Growth %. Lower score = more growth per valuation dollar.
- Forward P/S: Compare to TTM P/S. If forward P/S is dramatically lower than TTM, verify whether management guidance is credible before relying on it.
- Historical P/S range: Where does current valuation sit relative to the 3-year min, max, and average?
- Insider ownership and SBC: Is management significantly aligned with shareholders? Is stock-based compensation excessive relative to revenue?

SECTION 3 — MANDATORY BEAR CASE (Red-Team Every Position)
Confirmation bias is the biggest risk in investing. Before sizing any position above 10%, write an explicit 3-point short thesis and answer each point.
- Customer concentration: What % of revenue comes from the top 3 clients? Has it been rising or falling? Flag if any single customer exceeds 30% of revenue.
- Dilution risk: Are there any active ATM (at-the-market) equity programs or secondary offerings in the last 24 months? Continuous ATM = continuous dilution.
- Last earnings miss: When did the company last miss earnings? What was the reason and the stock's reaction?
- 10-K specific risks: Flag company-specific (not boilerplate) risk factors from the most recent SEC filing.
- Bull case critique: Why might the market be discounting this stock even if the thesis sounds compelling?

A position must survive the bear case to be sized above 10%. If the bear case is compelling and unresolved, limit to a starter position (≤7%) or avoid entirely.

SECTION 4 — TECHNICAL OVERLAY
Fundamentals tell you what to buy. Technicals help with price and timing.
- Key price levels (4.1): Identify key resistance levels (52-week high, swing highs, analyst price targets) and support levels (20-day, 100-day, 200-day SMA). What is the immediate level to watch? Is price near a critical breakout or breakdown zone?
- Moving averages (4.2): Is the stock above or below its 200-day MA? What is the slope — rising (bullish) or falling (bearish)? Has there been a recent Golden Cross (bullish) or Death Cross (bearish)? A sharply rising 200-day MA with price well above it signals strong trend momentum. A stock well below a falling 200-day MA is in structural downtrend — size cautiously.
- Relative Strength (4.3): Analyze RS vs SPY over the last 3 months. Is the RS line accelerating or breaking down on market pullbacks? An RS breakdown while the index rallies is a bearish divergence signal.
- Short interest (4.4): Check short interest as % of float and days to cover. Has it been rising or falling over the last 12 months? Low days-to-cover plus a positive upcoming catalyst = potential short squeeze opportunity. High short interest + deteriorating fundamentals = dangerous.
- Sentiment and volatility (4.5): What is the prevailing retail sentiment — overwhelmingly bullish, mixed, or fearful? How does current implied volatility compare to historical volatility? Are there any notable price/volume patterns — high-volume breakouts, breakdowns, or divergences worth flagging?

SECTION 5 — VERDICT
After running the full framework, state explicitly:
- Bull case (3 points)
- Bear case (3 points)
- Net view and conviction level (High / Medium / Low)
- What would change your mind in either direction?

========================
WEB SEARCH GUIDANCE
========================

You have live web search. Use it proactively to fill data gaps the provided holdings JSON cannot cover.

Search for each stock under review:
- §2 gaps: current P/S TTM and forward P/S, 3-year historical P/S range (min/max/avg), insider ownership %, SBC as % of revenue
- §3 gaps: revenue from top 3 customers (% of total), any ATM equity programs or secondary offerings filed in the last 24 months, most recent earnings miss (date, reason, stock reaction), company-specific 10-K risk factors (not boilerplate)
- §4.1: current analyst consensus price target, 52-week high, key swing levels
- §4.4: current short interest as % of float and days to cover (if not in holdings data)
- §4.5: current retail sentiment (Stocktwits/Reddit), implied volatility vs 1-year historical average, notable recent volume events

Do NOT search for data already in the holdings JSON: forward_pe, revenue_growth_yoy, gross_margin, operating_margin, sma200_pct, rs_vs_spy_qtd, short_pct_float.

Cite web-sourced data inline: "per SEC EDGAR:" / "per Fintel:" / "per Stocktwits:" / "per analyst consensus:".

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
            "52w_change":      info.get("52WeekChange"),
            "short_pct_float": info.get("shortPercentOfFloat"),  # decimal (0.008 = 0.8%)
            "days_to_earnings": days_to_earnings,
        }
    except Exception as exc:
        log.warning("yfinance error for %s: %s", ticker, exc)
        return {"ticker": ticker}


_FUNDAMENTAL_KEYS: frozenset[str] = frozenset({
    "forward_pe", "peg_ratio", "ev_ebitda", "roe", "profit_margin",
    "gross_margin", "operating_margin", "debt_to_equity", "52w_change",
})
_TECHNICAL_KEYS: frozenset[str] = frozenset({"sma200_pct", "short_pct_float", "perf_qtd", "rsi"})

_CRITICAL_DATA_FIELDS: tuple[str, ...] = (
    "rsi", "sma200_pct", "perf_qtd",      # technicals
    "forward_pe", "revenue_growth_yoy",   # core fundamentals
)


def compute_data_gaps(holding: dict) -> list[str]:
    """Return the sorted critical fields missing (None or absent) for a holding.

    Pure — no I/O. Empty list means the holding has full critical coverage.
    Minor/optional fields (e.g. short_pct_float) are intentionally not tracked.
    """
    return sorted(f for f in _CRITICAL_DATA_FIELDS if holding.get(f) is None)


def annotate_and_collect_gaps(enriched: list[dict]) -> dict[str, list[str]]:
    """Tag each holding that has critical data gaps and return {ticker: gaps}.

    Sets holding["_data_gaps"] in place (so it serializes into the prompt and
    is captured in the run log). Only holdings with >=1 gap appear in the map.
    """
    gaps_by_ticker: dict[str, list[str]] = {}
    for holding in enriched:
        gaps = compute_data_gaps(holding)
        if gaps:
            holding["_data_gaps"] = gaps
            gaps_by_ticker[holding.get("ticker", "?")] = gaps
    return gaps_by_ticker


def format_data_gap_field(gaps_by_ticker: dict[str, list[str]]) -> dict | None:
    """Return an embed field summarizing per-ticker data gaps, or None if empty."""
    if not gaps_by_ticker:
        return None
    parts = [f"{tk} ({', '.join(gaps)})" for tk, gaps in sorted(gaps_by_ticker.items())]
    return _field("⚠️ Data gaps", "; ".join(parts), inline=False)


def _fetch_technical_data(ticker: str) -> dict:
    """Compute Section 4 technical indicators from yfinance price history.

    Returns sma200_pct, rsi, and perf_qtd.  short_pct_float is already
    fetched in _fetch_yf_data (info["shortPercentOfFloat"]) so it is not
    duplicated here.
    """
    try:
        hist = yf.Ticker(ticker).history(period="1y")
        if hist.empty or len(hist) < 15:
            return {}

        closes = hist["Close"].astype(float)

        # ── SMA 200 (% above/below) ───────────────────────────────────────────
        sma200_pct = None
        if len(closes) >= 200:
            sma200 = closes.iloc[-200:].mean()
            if sma200 > 0:
                sma200_pct = round(closes.iloc[-1] / sma200 - 1, 4)

        # ── RSI (14) — Wilder smoothing ───────────────────────────────────────
        rsi = None
        if len(closes) >= 15:
            delta = closes.diff().dropna()
            gain  = delta.clip(lower=0)
            loss  = (-delta).clip(lower=0)
            avg_g = gain.ewm(com=13, min_periods=14).mean().iloc[-1]
            avg_l = loss.ewm(com=13, min_periods=14).mean().iloc[-1]
            if avg_l > 0:
                rsi = round(100 - 100 / (1 + avg_g / avg_l), 1)
            elif avg_g > 0:
                rsi = 100.0

        # ── Calendar-QTD performance ──────────────────────────────────────────
        perf_qtd = None
        today = date.today()
        q_month = ((today.month - 1) // 3) * 3 + 1
        quarter_start = date(today.year, q_month, 1)
        idx_dates = hist.index.date
        qtd_mask  = idx_dates >= quarter_start
        if qtd_mask.any():
            qtd_open = float(hist["Close"].iloc[int(qtd_mask.argmax())])
            if qtd_open > 0:
                perf_qtd = round(float(closes.iloc[-1]) / qtd_open - 1, 4)

        out = {"sma200_pct": sma200_pct, "rsi": rsi, "perf_qtd": perf_qtd}
        return {k: v for k, v in out.items() if v is not None}
    except Exception as exc:
        log.warning("technical data error for %s: %s", ticker, exc)
        return {}


_WEB_SEARCH_TOOL: dict = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 30,
}


def _call_claude_sync(user_message: str) -> str:
    """Agentic loop with live web search. Continues until Claude signals end_turn."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05",
        "content-type": "application/json",
    }
    messages: list[dict] = [{"role": "user", "content": user_message}]
    for _turn in range(80):
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": "claude-opus-4-8",
                "max_tokens": 16000,
                "system": _SYSTEM_PROMPT,
                "messages": messages,
                "tools": [_WEB_SEARCH_TOOL],
            },
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        content: list = data["content"]
        stop_reason: str = data.get("stop_reason", "end_turn")
        messages.append({"role": "assistant", "content": content})
        if stop_reason == "end_turn":
            return "\n".join(b["text"] for b in content if b.get("type") == "text")
        if stop_reason == "max_tokens":
            # Response was cut at the token budget — trade block is likely truncated.
            # Return what we have; _parse_trade_block will detect the incomplete JSON.
            log.error(
                "Claude hit max_tokens limit (%d) — response truncated; "
                "analysis may be incomplete and trade block may fail to parse",
                16000,
            )
            return "\n".join(b["text"] for b in content if b.get("type") == "text")
        if stop_reason == "tool_use":
            # web_search_20250305 is server-side: Anthropic embeds search results as
            # content blocks that carry a tool_use_id field (e.g. "web_search_result").
            # Collect any tool_use_id that already has a result block, so we only
            # stub out tool_use blocks the server has not yet resolved.
            resolved_ids = {b["tool_use_id"] for b in content if b.get("tool_use_id")}
            pending = [b for b in content if b.get("type") == "tool_use" and b.get("id") not in resolved_ids]
            if pending:
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": b["id"], "content": ""} for b in pending],
                })
            continue
        break
    log.warning("Claude agentic loop hit 80-turn safety cap — returning last assistant turn only")
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            texts = [
                b["text"] for b in (msg["content"] if isinstance(msg["content"], list) else [])
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


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

    # Attach the most recent full 5-section research so Claude can see its prior reasoning
    last_analysis = recent[-1].get("analysis_body") or ""
    if last_analysis:
        last_ts = recent[-1].get("timestamp", "unknown")[:10]
        lines.append(
            f"\n--- Prior 5-Section Research ({last_ts}) ---\n"
            f"Use this to track thesis evolution, flag broken theses, and avoid "
            f"repeating the same reasoning without new evidence.\n\n"
            f"{last_analysis}\n"
            f"--- End Prior Research ---"
        )

    # Weave in any weekly Inspection activity since the last rebalance, so the
    # monthly rebalance can build on what Inspection already decided instead
    # of re-deriving a thesis Inspection already updated.
    try:
        from app.claude_inspection import _load_recent_inspection_entries
        last_rebalance_ts = recent[-1].get("timestamp", "") if recent else ""
        inspection_entries = [
            e for e in _load_recent_inspection_entries(limit=10)
            if e.get("timestamp", "") > last_rebalance_ts
            and any(t for t in e.get("trades_executed", []))
        ]
        if inspection_entries:
            lines.append("\n--- Weekly Inspection Activity Since Last Rebalance ---")
            for entry in inspection_entries:
                ts = entry.get("timestamp", "unknown")[:10]
                for trade in entry.get("trades_executed", []):
                    ticker = trade.get("ticker", "?")
                    action = trade.get("action", "?")
                    note = entry.get("notes", {}).get(ticker, "")
                    lines.append(f"  {ts}: {action} {ticker} — {note}")
            lines.append("--- End Inspection Activity ---")
    except Exception as exc:
        log.warning("Could not load recent inspection activity: %s", exc)

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


_DISCORD_LIMIT = 1900  # Discord max is 2000; leave headroom for formatting


def _chunk_text(text: str, limit: int = _DISCORD_LIMIT) -> list[str]:
    """Split text into chunks that fit within Discord's character limit.
    Splits on the last newline within the limit so code blocks and sections
    stay intact. Falls back to a hard split only if no newline exists."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            # No newline in window — hard split, nothing to skip
            chunks.append(text[:limit])
            text = text[limit:]
        else:
            chunks.append(text[:split_at])
            # Advance past exactly the one split newline; preserve any subsequent
            # blank lines so section separators (\n\n) survive chunk boundaries
            text = text[split_at + 1:]
    return chunks


async def _send_chunked(notify_fn, text: str, delay: float = 0.55) -> None:
    """Send text as one or more ordered Discord messages, respecting the limit.
    Using a single coroutine guarantees chunk ordering — no race conditions."""
    for chunk in _chunk_text(text):
        await notify_fn(chunk)
        await asyncio.sleep(delay)


def _build_ki_decisions_summary(
    trades: list[dict],
    spy_price: Optional[float],
    month_label: str,
    holdings_count: int,
) -> str:
    """Build a clean monthly decisions card for the KI Server subscriber feed.
    No personal dollar amounts — portfolio $ stays Private Server only."""
    _ORDER  = ("SELL", "TRIM", "DOUBLE_DOWN", "BUY", "HOLD")
    _EMOJI  = {"SELL": "🔴", "TRIM": "✂️", "DOUBLE_DOWN": "🔥", "BUY": "🟢", "HOLD": "⏸"}
    _LABEL  = {"SELL": "SELL", "TRIM": "TRIM", "DOUBLE_DOWN": "DOUBLE DOWN", "BUY": "BUY", "HOLD": "HOLD"}
    sorted_trades = sorted(trades, key=lambda t: _ORDER.index(t["action"]) if t["action"] in _ORDER else 99)
    lines = [f"**REBALANCE DECISIONS — {month_label}**"]
    for t in sorted_trades:
        emoji  = _EMOJI.get(t["action"], "📌")
        label  = f"`{_LABEL.get(t['action'], t['action']):<11}`"
        ticker = f"**{t['ticker']:<5}**"
        weight = f"→ **{t['target_weight_pct']}%**" if t.get("target_weight_pct") is not None else "→ **EXIT**"
        lines.append(f"{emoji} {label} {ticker} {weight}")
    spy_str = f"SPY `${spy_price:,.2f}`  ·  " if spy_price else ""
    lines.append(f"\n{spy_str}`{holdings_count}` holdings  ·  Web-verified 5-section research")
    return "\n".join(lines)


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
    from app.notifications import (
        notify_claude_manager, notify_claude_manager_embed,
        notify_claude_manager_with_chart,
    )

    async def _post_financials_chart(tkr: str) -> None:
        """Fetch quarterly financials and post chart to manager + subscriber channels."""
        try:
            from app.financials_chart import fetch_quarterly_financials, generate_financials_chart
            loop = asyncio.get_running_loop()
            fin_data = await loop.run_in_executor(None, fetch_quarterly_financials, tkr)
            if fin_data is None:
                return
            chart_bytes = await loop.run_in_executor(None, generate_financials_chart, fin_data)
        except Exception as exc:
            log.warning("Financials chart generation failed for %s: %s", tkr, exc)
            return

        try:
            await notify_claude_manager_with_chart("", chart_bytes)
        except Exception as exc:
            log.warning("Financials chart → manager channel failed for %s: %s", tkr, exc)

        try:
            from app.notifications import notify_claude_signal_feed_with_chart
            await notify_claude_signal_feed_with_chart("", chart_bytes)
        except Exception as exc:
            log.warning("Financials chart → subscriber channel failed for %s: %s", tkr, exc)

    if not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set — skipping monthly rebalance")
        await notify_claude_manager_embed(_embed(
            "⚠️ REBALANCE SKIPPED — ANTHROPIC_API_KEY not configured",
            _CLR_ORANGE, footer=_timestamp(),
        ))
        return

    if not rh_client.available:
        log.warning("RH session unavailable — skipping monthly rebalance")
        await notify_claude_manager_embed(_embed(
            "⚠️ REBALANCE SKIPPED — Robinhood session is offline",
            _CLR_ORANGE, footer=_timestamp(),
        ))
        return

    await notify_claude_manager_embed(_embed(
        "🤖 KIMI PORTFOLIO MANAGER — MONTHLY REBALANCE",
        _CLR_YELLOW,
        description="Fetching portfolio data and running web-verified 5-section research analysis (up to 30 live searches)…",
        footer=_timestamp(),
    ))

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
            await notify_claude_manager_embed(_embed(
                "❌ REBALANCE FAILED — could not fetch portfolio",
                _CLR_RED, description=str(exc), footer=_timestamp(),
            ))
            return

        holdings_value = sum(p["qty"] * p.get("current_price", 0) for p in positions)
        portfolio_value = holdings_value + buying_power

        if portfolio_value < 1:
            log_entry["status"] = "skipped_zero_value"
            await notify_claude_manager_embed(_embed(
                "⚠️ REBALANCE SKIPPED — portfolio value is zero",
                _CLR_ORANGE, footer=_timestamp(),
            ))
            return


        # ── 2. Enrich holdings + fetch SPY, macro context, history in parallel ──
        from app.macro_context import fetch_macro_context

        yf_tasks    = [loop.run_in_executor(None, _fetch_yf_data, pos["symbol"]) for pos in positions]
        fv_tasks    = [loop.run_in_executor(None, _fetch_technical_data, pos["symbol"]) for pos in positions]
        spy_task    = loop.run_in_executor(None, _fetch_spy_price)
        spy_fv_task = loop.run_in_executor(None, _fetch_technical_data, "SPY")

        all_results = await asyncio.gather(
            *yf_tasks, *fv_tasks, spy_task, spy_fv_task, fetch_macro_context(),
            return_exceptions=True,
        )
        n           = len(positions)
        yf_results  = all_results[:n]
        fv_results  = all_results[n:2 * n]
        spy_price   = all_results[2 * n] if not isinstance(all_results[2 * n], Exception) else None
        spy_fv      = all_results[2 * n + 1] if not isinstance(all_results[2 * n + 1], Exception) else {}
        macro_text  = all_results[2 * n + 2] if not isinstance(all_results[2 * n + 2], Exception) else "Macro context unavailable."
        spy_perf_qtd = spy_fv.get("perf_qtd")

        if fv_results and all(r == {} for r in fv_results):
            log.warning("All technical data fetches returned empty — Section 4 data absent")
            await notify_claude_manager_embed(_embed(
                "⚠️ TECHNICAL DATA UNAVAILABLE",
                _CLR_ORANGE,
                description=(
                    "Could not compute technical indicators (200-day MA, RSI, QTD performance) "
                    "for any holding. Claude will analyze fundamentals only."
                ),
                footer=_timestamp(),
            ))

        all_history_records, history_text = _load_recent_history()

        enriched = []
        for pos, yf_data, fv_data in zip(positions, yf_results, fv_results):
            if isinstance(yf_data, Exception):
                yf_data = {"ticker": pos["symbol"]}
            data = dict(yf_data)
            for k in _FUNDAMENTAL_KEYS:
                if data.get(k) is None and fv_data.get(k) is not None:
                    data[k] = fv_data[k]
            for k in _TECHNICAL_KEYS:
                if fv_data.get(k) is not None:
                    data[k] = fv_data[k]
            if data.get("perf_qtd") is not None and spy_perf_qtd is not None:
                data["rs_vs_spy_qtd"] = round(data["perf_qtd"] - spy_perf_qtd, 4)
            weight_pct = round(
                pos["qty"] * pos.get("current_price", 0) / portfolio_value * 100, 1
            )
            enriched.append({
                **data,
                "qty": pos["qty"],
                "avg_entry_price": pos["avg_entry_price"],
                "current_price": pos.get("current_price"),
                "unrealized_pnl": round(pos.get("unrealized_pl", 0), 2),
                "unrealized_pnl_pct": round(pos.get("unrealized_plpc", 0), 2),
                "current_weight_pct": weight_pct,
            })

        log_entry["portfolio_value"] = portfolio_value
        log_entry["buying_power"] = buying_power
        data_gaps_by_ticker = annotate_and_collect_gaps(enriched)
        log_entry["positions_before"] = enriched
        log_entry["spy_price_at_rebalance"] = spy_price

        # Decision Review is monitoring-only: score past trades and post the
        # monthly Discord review, but do NOT feed the scorecard back into the
        # prompt — Claude decides without reference to its own track record.
        scorecard = await loop.run_in_executor(None, build_live_scorecard)
        if scorecard.outcomes:
            await notify_claude_manager_embed(format_scorecard_embed(scorecard))

        # ── 3. Build prompt and call Claude ───────────────────────────────────
        prompt = (
            f"Today is the monthly portfolio rebalance date.\n\n"
            f"Total Portfolio Value: ${portfolio_value:,.2f}\n"
            f"Available Cash/Buying Power: ${buying_power:,.2f} "
            f"({buying_power / portfolio_value * 100:.1f}% of portfolio)\n\n"
            f"{macro_text}\n\n"
            f"{history_text}\n\n"
            f"Current Holdings:\n{json.dumps(enriched, indent=2)}\n\n"
            f"Please run the full research framework for each stock:\n\n"
            f"1. SCORE CURRENT HOLDINGS — For each position, apply the full 5-section research "
            f"framework: foundation (moat, catalysts), valuation rigor (Rule of 40, Value/Growth "
            f"Score, Forward P/S vs TTM, historical range), mandatory bear case (customer "
            f"concentration, dilution risk, last earnings miss, 10-K risks), technical overlay "
            f"(key price levels, moving averages + Golden/Death Cross, RS vs SPY, short interest "
            f"+ days to cover, sentiment + implied volatility + volume patterns), and a final "
            f"verdict (bull case, bear case, net view).\n\n"
            f"2. IDENTIFY NEW CANDIDATES — Screen for superior opportunities not currently held. "
            f"For each serious candidate, run all 5 research sections. A new position must survive "
            f"the full bear case before being sized above 10%.\n\n"
            f"3. DETERMINE OPTIMAL PORTFOLIO — Build the best portfolio for the next month. "
            f"Any position sized above 10% must have a resolved bear case documented above.\n\n"
            f"4. OUTPUT FORMAT — Write for Discord (markdown + native headers supported):\n"
            f"   • Before each stock, place a blank line then the divider: `══════════════════════════════`\n"
            f"   • Stock header (line immediately after divider): `## TICKER — Company Name`\n"
            f"   • Weight line (next line): `Current: X%  →  Target: Y%  |  Action: HOLD/BUY/SELL/TRIM`\n"
            f"   • Each section — blank line, then emoji + bold label, then content on next lines:\n"
            f"       🔬 **§1 FOUNDATION**\n"
            f"       📊 **§2 VALUATION RIGOR**\n"
            f"       🐻 **§3 BEAR CASE**\n"
            f"       📈 **§4 TECHNICAL OVERLAY** — 4.1 Key Levels · 4.2 MAs + Golden/Death Cross · 4.3 RS vs SPY · 4.4 Short Interest · 4.5 Sentiment & Vol\n"
            f"       ⚖️ **§5 VERDICT**\n"
            f"   • Use inline code for numbers: `Rule of 40: 68` `P/S: 8.2x` `+23% YoY`\n"
            f"   • Cite web-sourced data inline: `per Fintel:` `per SEC EDGAR:` `per Stocktwits:` etc.\n"
            f"   • Conviction line (last line of §5): **Conviction: HIGH** | **MEDIUM** | **LOW**\n"
            f"   • After all analysis, end with the required JSON block."
        )

        try:
            log.info("Calling Claude Opus for monthly portfolio rebalance...")
            response_text = await loop.run_in_executor(None, _call_claude_sync, prompt)
        except Exception as exc:
            log.error("Claude API call failed: %s", exc)
            log_entry["status"] = "failed_claude_api"
            await notify_claude_manager_embed(_embed(
                "❌ REBALANCE FAILED — Anthropic API error",
                _CLR_RED, description=str(exc), footer=_timestamp(),
            ))
            return

        log_entry["claude_response"] = response_text

        # ── 4. Parse trade block ──────────────────────────────────────────────
        trade_block = _parse_trade_block(response_text)
        log_entry["trade_block"] = trade_block

        # Full analysis → main Discord + subscriber feed (chunked with rate-limit delays)
        analysis_body = re.sub(r"```json.*?```", "", response_text, flags=re.DOTALL).strip()
        log_entry["analysis_body"] = analysis_body  # persisted for next month's history lookback
        spy_str = f"${spy_price:,.2f}" if spy_price else "—"
        cash_pct = buying_power / portfolio_value * 100 if portfolio_value > 0 else 0
        _gap_field = format_data_gap_field(data_gaps_by_ticker)
        analysis_header = _embed(
            "📊 KIMI MONTHLY PORTFOLIO ANALYSIS",
            _CLR_YELLOW,
            description=(
                f"Full 5-section research completed for **{len(positions)} position(s)** + new candidates.\n\n"
                f"**Portfolio Value** `${portfolio_value:,.2f}`  ·  "
                f"**Cash** `${buying_power:,.2f} ({cash_pct:.1f}%)`  ·  "
                f"**SPY** `{spy_str}`  ·  "
                f"**Holdings** `{len(positions)}`"
            ),
            fields=[_gap_field] if _gap_field else None,
            footer=_timestamp(),
        )
        await asyncio.sleep(0.8)
        await notify_claude_manager_embed(analysis_header)

        # Send each ticker's thesis as its own message, immediately followed by
        # its financials chart. Sequential awaits guarantee ordering — no racing.
        from app.notifications import notify_claude_signal_feed

        ticker_sections = [s.strip() for s in analysis_body.split(_DIVIDER) if s.strip()]
        for section in ticker_sections:
            # _send_chunked handles splitting + inter-chunk delay; chart posts after
            await _send_chunked(notify_claude_manager, section)
            section_tkr = _section_ticker(section)
            if section_tkr:
                await _post_financials_chart(section_tkr)

        # KI Server task is defined below after `trades` is parsed so research and
        # decisions card fire in one coroutine — no ordering race possible.

        if trade_block is None:
            log_entry["status"] = "failed_parse"
            await asyncio.sleep(0.8)
            await notify_claude_manager_embed(_embed(
                "⚠️ Could not parse trade instructions",
                _CLR_ORANGE,
                description="No trades executed — review the analysis above manually.",
                footer=_timestamp(),
            ))
            return

        if trade_block.get("no_changes"):
            log_entry["status"] = "no_changes"
            benchmark_str = _format_benchmark(log_entry, all_history_records)
            await asyncio.sleep(0.8)
            await notify_claude_manager_embed(_embed(
                "✅ NO CHANGES THIS MONTH",
                _CLR_GREEN,
                description=(
                    "Kimi Portfolio Manager determined the current portfolio requires no rebalancing."
                    + (f"\n\n{benchmark_str}" if benchmark_str else "")
                ),
                footer=_timestamp(),
            ))
            return

        _EXCLUDED = {"SPY"}  # managed by Kimi — Claude must never touch these

        trades = [t for t in trade_block.get("trades", []) if t.get("ticker", "").upper() not in _EXCLUDED]

        # Risk guardrails: clamp any position to <=25% (safety-critical, always),
        # then best-effort sector-concentration alert (>50%, no auto-scale).
        _clamps = clamp_position_weights(trades)
        _warnings: list = []
        _unknown: list = []
        try:
            # Offload the (bounded, blocking) yfinance sector lookups off the
            # event loop, matching the run_in_executor pattern used above.
            _sector_map, _unknown = await loop.run_in_executor(
                None, resolve_sectors, enriched, trades, _yf_sector_fetch
            )
            _warnings = sector_warnings(
                compute_sector_exposure(positions, trades, portfolio_value, _sector_map.get)
            )
        except Exception as exc:
            log.warning("Rebalance sector guardrail check failed: %s", exc)
        _guardrail_embed = format_guardrail_embed(_clamps, _warnings, _unknown)
        if _guardrail_embed:
            await notify_claude_manager_embed(_guardrail_embed)

        # KI Server: research sections then decisions card — single task, guaranteed ordering
        _ki_month = datetime.now(_CT).strftime("%B %Y").upper()
        async def _ki_full_task(
            _secs=ticker_sections, _mo=_ki_month,
            _tr=trades, _spy=spy_price, _cnt=len(positions),
        ) -> None:
            await notify_claude_signal_feed(f"📊 **KIMI MONTHLY PORTFOLIO ANALYSIS — {_mo}**")
            await asyncio.sleep(0.55)
            for sec in _secs:
                await _send_chunked(notify_claude_signal_feed, sec)
            await asyncio.sleep(0.55)
            await notify_claude_signal_feed(_build_ki_decisions_summary(_tr, _spy, _mo, _cnt))
        _fire(_ki_full_task())

        if not trades:
            log_entry["status"] = "no_trades"
            await asyncio.sleep(0.8)
            await notify_claude_manager_embed(_embed(
                "✅ No trades to execute this month",
                _CLR_GREEN,
                footer=_timestamp(),
            ))
            return

        action_count = len([t for t in trades if t["action"] != "HOLD"])
        await asyncio.sleep(1.5)   # clear gap after all thesis+chart messages settle
        await notify_claude_manager_embed(_embed(
            f"⚡ EXECUTING {action_count} TRADE(S)",
            _CLR_YELLOW,
            description=f"Sells and trims execute first to fund buys.",
            footer=_timestamp(),
        ))

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
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_embed(
                    f"ℹ️ KIMI SELL — {ticker} skipped",
                    _CLR_GRAY,
                    description=result["note"],
                    footer=_timestamp(),
                ))
                log_entry["trades_skipped"].append({"action": "SELL", "ticker": ticker, "reason": result["note"]})
                continue

            if result.get("status") != "ok" or not result.get("qty"):
                reason = result.get("reason", "unknown")
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_embed(
                    f"❌ KIMI SELL — {ticker} FAILED",
                    _CLR_RED,
                    description=reason,
                    footer=_timestamp(),
                ))
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

            await asyncio.sleep(0.8)
            if queued:
                await notify_claude_manager_embed(_trade_embed(
                    "SELL", ticker,
                    [
                        _field("Status", "⏳ Queued for market open"),
                        _field("Est. Qty", f"{qty:g} shares"),
                        _field("Est. Price", f"${result.get('price_est', 0):,.2f}"),
                    ],
                    _timestamp(),
                ))
            else:
                pnl_str = (
                    f"+${dollar_pnl:,.2f} (+{pct_pnl:.2f}%) 🟢 WIN"
                    if dollar_pnl >= 0
                    else f"-${abs(dollar_pnl):,.2f} (-{abs(pct_pnl):.2f}%) 🔴 LOSS"
                ) if dollar_pnl is not None else "—"
                await notify_claude_manager_embed(_trade_embed(
                    "SELL", ticker,
                    [
                        _field("Qty",    f"{qty:g} shares @ ${fill or 0:,.2f}"),
                        _field("Record", f"{wins}W — {losses}L"),
                        _field("P&L",    pnl_str, inline=False),
                    ],
                    _timestamp(),
                ))
                from app.notifications import notify_claude_signal_feed
                _fire(notify_claude_signal_feed(
                    f"🔴 **KIMI SELL — {ticker}**\n"
                    f"@ ${fill or 0:,.2f}\n"
                    + ("🟢 WIN" if dollar_pnl >= 0 else "🔴 LOSS") + "\n"
                    + _timestamp()
                ))

        # ── 5b. Execute TRIMs ─────────────────────────────────────────────────
        for trade in (t for t in trades if t["action"] == "TRIM"):
            ticker = trade["ticker"].upper()
            target_wt = trade.get("target_weight_pct", 5)
            target_value = portfolio_value * target_wt / 100

            pos = next((p for p in positions if p["symbol"] == ticker), None)
            if pos is None:
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_embed(
                    f"⚠️ TRIM {ticker} skipped — no open position",
                    _CLR_GRAY, footer=_timestamp(),
                ))
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": "no position"})
                continue

            current_qty   = pos["qty"]
            current_price = pos.get("current_price", 0)
            current_value = current_qty * current_price

            if current_qty < 1.0:
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_embed(
                    f"⚠️ TRIM {ticker} skipped — fractional position",
                    _CLR_GRAY,
                    description=(
                        f"Position is {current_qty:.4f} shares (< 1 whole share). "
                        "Robinhood cannot partially sell fractional positions. Use SELL to close entirely."
                    ),
                    footer=_timestamp(),
                ))
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": f"fractional ({current_qty:.4f} shares)"})
                continue

            if target_value >= current_value * 0.95:
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_embed(
                    f"⚠️ TRIM {ticker} skipped — already at target",
                    _CLR_GRAY,
                    description=f"Position already at or below {target_wt}% target weight.",
                    footer=_timestamp(),
                ))
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": "already at target"})
                continue

            sell_qty = round((current_value - target_value) / current_price, 6) if current_price > 0 else 0.0
            if sell_qty <= 0:
                log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": "sell qty <= 0"})
                continue

            result = await rh_client.sell_shares_async(ticker, sell_qty)

            if result.get("status") != "ok":
                reason = result.get("reason", "unknown")
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_embed(
                    f"❌ KIMI TRIM — {ticker} FAILED",
                    _CLR_RED, description=reason, footer=_timestamp(),
                ))
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

            await asyncio.sleep(0.8)
            if queued:
                await notify_claude_manager_embed(_trade_embed(
                    "TRIM", ticker,
                    [
                        _field("Status",        "⏳ Queued for market open"),
                        _field("Selling",       f"{qty_sold:g} shares"),
                        _field("Target Weight", f"{target_wt}%"),
                    ],
                    _timestamp(),
                ))
            else:
                pnl_str = (
                    f"+${dollar_pnl:,.2f} (+{pct_pnl:.2f}%) 🟢 WIN"
                    if dollar_pnl >= 0
                    else f"-${abs(dollar_pnl):,.2f} (-{abs(pct_pnl):.2f}%) 🔴 LOSS"
                ) if dollar_pnl is not None else "—"
                await notify_claude_manager_embed(_trade_embed(
                    "TRIM", ticker,
                    [
                        _field("Sold",          f"{qty_sold:g} shares @ ${fill or 0:,.2f}"),
                        _field("→ Target",      f"{target_wt}%"),
                        _field("Record",        f"{wins}W — {losses}L"),
                        _field("P&L",           pnl_str, inline=False),
                    ],
                    _timestamp(),
                ))
                from app.notifications import notify_claude_signal_feed
                _fire(notify_claude_signal_feed(
                    f"✂️ **KIMI TRIM — {ticker}**\n"
                    f"@ ${fill or 0:,.2f} → target {target_wt}% weight\n"
                    + (f"+{pct_pnl:.2f}% 🟢 WIN" if dollar_pnl >= 0 else f"-{abs(pct_pnl):.2f}% 🔴 LOSS") + "\n"
                    + _timestamp()
                ))

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
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_embed(
                    f"⚠️ KIMI {action_label} — {ticker} skipped",
                    _CLR_GRAY, description=reason, footer=_timestamp(),
                ))
                log_entry["trades_skipped"].append({"action": action_label, "ticker": ticker, "reason": reason})
                continue

            result = await rh_client.buy_dollars_async(ticker, invest_dollars)

            if result.get("status") != "ok":
                reason = result.get("reason", "unknown")
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_embed(
                    f"❌ KIMI {action_label} — {ticker} FAILED",
                    _CLR_RED, description=reason, footer=_timestamp(),
                ))
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

            await asyncio.sleep(0.8)
            if queued:
                await notify_claude_manager_embed(_trade_embed(
                    action_label, ticker,
                    [
                        _field("Status",        "⏳ Queued for market open"),
                        _field("Est. Qty",      f"{qty:g} shares ≈ ${est:,.2f}"),
                        _field("Target Weight", f"{target_wt}%"),
                        _field("Investing",     f"${invest_dollars:,.2f}"),
                    ],
                    _timestamp(),
                ))
            else:
                await notify_claude_manager_embed(_trade_embed(
                    action_label, ticker,
                    [
                        _field("Qty",           f"{qty:g} shares @ ${fill or 0:,.2f}"),
                        _field("Target Weight", f"{target_wt}%"),
                        _field("Invested",      f"${invest_dollars:,.2f}"),
                    ],
                    _timestamp(),
                ))
                from app.notifications import notify_claude_signal_feed
                sig_emoji = "🔥" if action_label == "DOUBLE_DOWN" else "🟢"
                _fire(notify_claude_signal_feed(
                    f"{sig_emoji} **KIMI {action_label} — {ticker}**\n"
                    f"@ ${fill or 0:,.2f}\n"
                    f"Target: {target_wt}% weight\n"
                    + _timestamp()
                ))
            available_budget = max(0.0, available_budget - invest_dollars)

        log_entry["status"] = "completed"

        # ── 7. Completion embed with benchmark ────────────────────────────────
        benchmark_str = _format_benchmark(log_entry, all_history_records)
        executed_count = len(log_entry["trades_executed"])
        skipped_count  = len(log_entry["trades_skipped"])

        _action_order = ("SELL", "TRIM", "DOUBLE_DOWN", "BUY", "HOLD")
        _action_emoji = {"SELL": "🔴", "TRIM": "✂️", "DOUBLE_DOWN": "🔥", "BUY": "🟢", "HOLD": "⏸"}
        trade_parts = [
            f"{_action_emoji.get(a, '')} {sum(1 for t in log_entry['trades_executed'] if t['action'] == a)}× {a}"
            for a in _action_order
            if any(t["action"] == a for t in log_entry["trades_executed"])
        ]
        trade_summary = "  ·  ".join(trade_parts) if trade_parts else "No trades"

        desc_lines = [f"**{trade_summary}**"]
        if benchmark_str:
            desc_lines.append(f"\n{benchmark_str}")
        if skipped_count:
            desc_lines.append(f"\n⚠️ {skipped_count} trade(s) skipped — see messages above.")
        await asyncio.sleep(0.8)
        await notify_claude_manager_embed(_embed(
            "✅ KIMI PORTFOLIO REBALANCE COMPLETE",
            _CLR_GREEN,
            description="\n".join(desc_lines),
            footer=_timestamp(),
        ))

    finally:
        _append_rebalance_log(log_entry)
