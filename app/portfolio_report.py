"""
portfolio_report.py — Robinhood portfolio snapshot with pie chart and valuation ratings.

Fetches Robinhood positions and buying power, generates a cyberpunk-style donut
chart, and rates the portfolio on current valuation (Forward P/E) and potential
upside (analyst price targets) via yfinance.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytz
import yfinance as yf

log = logging.getLogger(__name__)

_CT = pytz.timezone("America/Chicago")

# ── Cyberpunk palette (matches chart.py) ──────────────────────────────────────
_BG         = "#030303"
_PANEL      = "#0a0a0a"
_TEXT_COLOR = "#d0d0f0"
_TITLE_COLOR = "#ffe600"
_ZERO_COLOR = "#2a2a2a"

_NEON_COLORS = [
    "#ffe600", "#39ff14", "#ff2d78", "#00f7ff", "#ff6600",
    "#bf00ff", "#00ff9f", "#ff9900", "#ff00ff", "#00ffff",
    "#ff0050", "#ffff00",
]
_CASH_COLOR = "#2e2e2e"

_CACHE_PATH = "/data/rh_positions_cache.json"


# ── Position cache (fallback when RH API is down) ─────────────────────────────

def _save_rh_cache(positions: list, buying_power: float) -> None:
    try:
        with open(_CACHE_PATH, "w") as f:
            json.dump({
                "timestamp": datetime.now(_CT).isoformat(),
                "positions": positions,
                "buying_power": buying_power,
            }, f)
    except Exception as exc:
        log.warning("Failed to save RH position cache: %s", exc)


def _load_rh_cache() -> tuple[list, float, str] | None:
    """Returns (positions, buying_power, cached_at_str) or None if no cache."""
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        ts = datetime.fromisoformat(data["timestamp"])
        cached_at = f"{ts.strftime('%B')} {ts.day}, {ts.year} {int(ts.strftime('%I'))}:{ts.strftime('%M %p')} CT"
        return data["positions"], data["buying_power"], cached_at
    except Exception:
        return None


# ── Data fetching ─────────────────────────────────────────────────────────────

async def _fetch_all_holdings() -> tuple[dict[str, float], float, str | None]:
    """
    Returns (holdings, total_value, cache_notice).
    cache_notice is None when using live data, or a warning string when falling
    back to the on-disk position cache because the RH API was unreachable.
    """
    from app.trading.robinhood_client import rh_client

    holdings: dict[str, float] = {}
    cache_notice: str | None = None

    try:
        rh_positions = await rh_client.get_all_positions_async()
        rh_cash      = await rh_client.get_buying_power_async() or 0.0
        for pos in rh_positions:
            val = pos["qty"] * pos.get("current_price", 0)
            if val > 0:
                holdings[pos["symbol"]] = holdings.get(pos["symbol"], 0) + val
        if rh_cash > 0:
            holdings["Cash"] = rh_cash
        _save_rh_cache(rh_positions, rh_cash)
    except Exception as exc:
        log.warning("Portfolio snapshot: Robinhood fetch failed: %s — trying cache", exc)
        cached = _load_rh_cache()
        if cached:
            rh_positions, rh_cash, cached_at = cached
            for pos in rh_positions:
                val = pos["qty"] * pos.get("current_price", 0)
                if val > 0:
                    holdings[pos["symbol"]] = holdings.get(pos["symbol"], 0) + val
            if rh_cash > 0:
                holdings["Cash"] = rh_cash
            cache_notice = f"⚠️ RH API unavailable — showing cached data from {cached_at}"

    total = sum(holdings.values())
    return holdings, total, cache_notice


def _fetch_yf_valuation(ticker: str) -> dict:
    """Fetch forward P/E and analyst target for a single ticker."""
    try:
        info = yf.Ticker(ticker).info
        current = info.get("currentPrice") or info.get("regularMarketPrice")
        target  = info.get("targetMeanPrice")
        fwd_pe  = info.get("forwardPE")
        upside  = ((target - current) / current * 100) if target and current else None
        return {"ticker": ticker, "forward_pe": fwd_pe, "upside_pct": upside}
    except Exception:
        return {"ticker": ticker, "forward_pe": None, "upside_pct": None}


# ── Chart generation ──────────────────────────────────────────────────────────

def _generate_donut_chart(holdings: dict[str, float], total: float, date_str: str) -> bytes:
    # Sort: stocks first (alphabetically), cash last
    stocks = sorted((k, v) for k, v in holdings.items() if k != "Cash")
    cash   = [("Cash", holdings["Cash"])] if "Cash" in holdings else []
    items  = stocks + cash

    labels = []
    sizes  = []
    colors = []
    color_idx = 0

    for ticker, value in items:
        pct = value / total * 100
        labels.append(f"{ticker}\n${value:,.0f}  ({pct:.1f}%)")
        sizes.append(value)
        if ticker == "Cash":
            colors.append(_CASH_COLOR)
        else:
            colors.append(_NEON_COLORS[color_idx % len(_NEON_COLORS)])
            color_idx += 1

    fig, ax = plt.subplots(figsize=(11, 8))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    wedges, texts = ax.pie(
        sizes,
        labels=None,
        colors=colors,
        startangle=90,
        wedgeprops={"width": 0.55, "edgecolor": _BG, "linewidth": 2},
        pctdistance=0.82,
    )

    # Glow effect on wedges
    for wedge in wedges:
        wedge.set_alpha(0.92)

    # Center text
    ax.text(0, 0.08, "TOTAL", ha="center", va="center",
            color=_TEXT_COLOR, fontsize=11, fontweight="bold")
    ax.text(0, -0.12, f"${total:,.2f}", ha="center", va="center",
            color=_TITLE_COLOR, fontsize=16, fontweight="bold")

    # Legend
    legend_labels = [f"{lbl.split(chr(10))[0]}  —  {lbl.split(chr(10))[1]}" for lbl in labels]
    legend = ax.legend(
        wedges, legend_labels,
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        facecolor="#0a0a22",
        edgecolor=_ZERO_COLOR,
        labelcolor=_TEXT_COLOR,
        fontsize=10,
        framealpha=0.90,
        title="Holdings",
        title_fontsize=11,
    )
    legend.get_title().set_color(_TITLE_COLOR)

    ax.set_title(
        f"Portfolio Snapshot — {date_str}",
        fontsize=15, fontweight="bold",
        color=_TITLE_COLOR, pad=18,
    )

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Valuation analysis ────────────────────────────────────────────────────────

def _valuation_rating(weighted_pe: Optional[float]) -> str:
    if weighted_pe is None:
        return "N/A"
    if weighted_pe < 18:
        return f"🟢 Undervalued ({weighted_pe:.1f}x forward P/E)"
    if weighted_pe < 28:
        return f"🟡 Fair Value ({weighted_pe:.1f}x forward P/E)"
    if weighted_pe < 38:
        return f"🟠 Moderately Rich ({weighted_pe:.1f}x forward P/E)"
    return f"🔴 Expensive ({weighted_pe:.1f}x forward P/E)"


def _upside_rating(weighted_upside: Optional[float]) -> str:
    if weighted_upside is None:
        return "N/A"
    sign = "+" if weighted_upside >= 0 else ""
    if weighted_upside > 25:
        return f"🟢🟢 Strong ({sign}{weighted_upside:.1f}% to analyst targets)"
    if weighted_upside > 12:
        return f"🟢 Good ({sign}{weighted_upside:.1f}% to analyst targets)"
    if weighted_upside > 3:
        return f"🟡 Moderate ({sign}{weighted_upside:.1f}% to analyst targets)"
    return f"🔴 Limited ({sign}{weighted_upside:.1f}% to analyst targets)"


async def _build_valuation_text(holdings: dict[str, float], total: float) -> str:
    loop = asyncio.get_running_loop()
    stock_holdings = {k: v for k, v in holdings.items() if k != "Cash"}
    stock_total = sum(stock_holdings.values())

    if not stock_holdings or stock_total == 0:
        return "No stock positions to rate."

    # Fetch yfinance data concurrently
    results = await asyncio.gather(
        *[loop.run_in_executor(None, _fetch_yf_valuation, t) for t in stock_holdings]
    )

    # Weighted averages (weight = position value / total stock value)
    weighted_pe     = 0.0
    weighted_upside = 0.0
    pe_weight_sum   = 0.0
    up_weight_sum   = 0.0
    stock_lines     = []

    for r in results:
        ticker = r["ticker"]
        weight = stock_holdings[ticker] / stock_total
        fwd_pe = r["forward_pe"]
        upside = r["upside_pct"]

        if fwd_pe and fwd_pe > 0:
            weighted_pe   += fwd_pe * weight
            pe_weight_sum += weight
        if upside is not None:
            weighted_upside += upside * weight
            up_weight_sum   += weight

        pe_str  = f"{fwd_pe:.1f}x" if fwd_pe else "N/A"
        up_str  = (f"+{upside:.1f}%" if upside and upside >= 0 else f"{upside:.1f}%") if upside is not None else "N/A"
        stock_lines.append(f"  {ticker:<6} P/E: {pe_str:<8} Analyst upside: {up_str}")

    final_pe     = weighted_pe / pe_weight_sum if pe_weight_sum > 0 else None
    final_upside = weighted_upside / up_weight_sum if up_weight_sum > 0 else None

    lines = [
        f"**Current Valuation:** {_valuation_rating(final_pe)}",
        f"**Potential Upside:**  {_upside_rating(final_upside)}",
        f"*(S&P 500 benchmark: ~21x forward P/E)*",
        "",
        "**Per position:**",
    ] + stock_lines

    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

async def send_portfolio_snapshot() -> None:
    """Generate and post the full portfolio pie chart + valuation ratings."""
    from app.notifications import notify_portfolio_snapshot

    holdings, total, cache_notice = await _fetch_all_holdings()

    if total < 1:
        await notify_portfolio_snapshot("⚠️ **PORTFOLIO SNAPSHOT** — No positions found (RH API unavailable, no cache on disk)", b"")
        return

    now      = datetime.now(_CT)
    date_str = f"{now.strftime('%B')} {now.day}, {now.year}"
    ts_str   = f"🕐 {int(now.strftime('%I'))}:{now.strftime('%M %p')} {now.strftime('%Z')}"

    # Generate chart and valuation concurrently
    loop = asyncio.get_running_loop()
    chart_bytes, valuation_text = await asyncio.gather(
        loop.run_in_executor(None, _generate_donut_chart, holdings, total, date_str),
        _build_valuation_text(holdings, total),
    )

    message = (
        f"📊 **PORTFOLIO SNAPSHOT — {date_str}**\n"
        f"Total Value: **${total:,.2f}**\n\n"
        f"{valuation_text}\n\n"
        + (f"{cache_notice}\n" if cache_notice else "")
        + ts_str
    )

    await notify_portfolio_snapshot(message, chart_bytes)
