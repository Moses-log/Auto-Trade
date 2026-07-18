"""
macro_context.py — Fetch macro indicators for the Claude rebalance prompt.

Fetches VIX and 10Y Treasury yield via yfinance (already a dependency).
Fetches CPI YoY from FRED if FRED_API_KEY is configured (free API key).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
import yfinance as yf

from app.anthropic_cache import cached_system, apply_message_cache
from app.config import settings

log = logging.getLogger(__name__)


def _fetch_vix_yield_sync() -> dict:
    result: dict = {}
    try:
        result["vix"] = round(float(yf.Ticker("^VIX").fast_info["lastPrice"]), 2)
    except Exception as exc:
        log.warning("VIX fetch failed: %s", exc)
    try:
        result["ten_year_yield"] = round(float(yf.Ticker("^TNX").fast_info["lastPrice"]), 3)
    except Exception as exc:
        log.warning("10Y yield fetch failed: %s", exc)
    return result


_GEO_WEB_SEARCH_TOOL: dict = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}

_GEO_BRIEF_SYSTEM = (
    "You are a macro/geopolitical desk analyst. Using live web search, summarize the "
    "current macro and geopolitical developments most relevant to US equity markets "
    "right now: active or escalating armed conflicts, major elections, central-bank and "
    "fiscal policy shifts, sanctions, tariffs and trade actions, and commodity/energy "
    "shocks. Output 5 to 8 neutral, factual one-line bullets, each starting with '- '. "
    "State only what is happening and its scale; do NOT give investment advice, price "
    "targets, sector calls, or any market-direction opinion. If nothing materially new "
    "is happening, reply with a single bullet saying the backdrop is quiet."
)


def _fetch_geopolitical_brief_sync() -> Optional[str]:
    """One server-side web-search call to Claude for a neutral macro/geopolitical brief.

    Returns a short bullet block, or None if the key is unset or anything fails —
    the caller degrades gracefully to an 'unavailable' line. Rebalance-only by
    design (see fetch_macro_context's include_geopolitical flag); never wired into
    the weekly Inspection, which must stay noise-resistant.
    """
    if not settings.anthropic_api_key:
        return None
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05",
        "content-type": "application/json",
    }
    system_payload = cached_system(_GEO_BRIEF_SYSTEM)
    messages: list[dict] = [
        {"role": "user", "content": "Give the current macro/geopolitical backdrop brief."}
    ]
    try:
        for _turn in range(12):
            apply_message_cache(messages)  # roll the cache breakpoint to the newest turn
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json={
                    # Sonnet: headline summarization needs no Opus-tier reasoning,
                    # and this runs monthly — cheaper + faster with no quality loss.
                    # The rebalance itself still runs on Opus (claude_manager.py).
                    "model": "claude-sonnet-5",
                    "max_tokens": 1500,
                    "system": system_payload,
                    "messages": messages,
                    "tools": [_GEO_WEB_SEARCH_TOOL],
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            content: list = data["content"]
            stop_reason: str = data.get("stop_reason", "end_turn")
            messages.append({"role": "assistant", "content": content})
            if stop_reason in ("end_turn", "max_tokens"):
                text = "\n".join(
                    b["text"] for b in content if b.get("type") == "text"
                ).strip()
                return text or None
            if stop_reason == "tool_use":
                # web_search is server-side; stub only tool_use blocks the server
                # has not already resolved (mirrors claude_manager._call_claude_sync).
                resolved_ids = {b["tool_use_id"] for b in content if b.get("tool_use_id")}
                pending = [
                    b for b in content
                    if b.get("type") == "tool_use" and b.get("id") not in resolved_ids
                ]
                if pending:
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": b["id"], "content": ""}
                            for b in pending
                        ],
                    })
                continue
            break
    except Exception as exc:
        log.warning("Geopolitical brief fetch failed: %s", exc)
    return None


def _fetch_cpi_sync() -> Optional[float]:
    """Returns CPI YoY % change using FRED CPIAUCSL series, or None if no key."""
    if not settings.fred_api_key:
        return None
    try:
        resp = httpx.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "CPIAUCSL",
                "api_key": settings.fred_api_key,
                "sort_order": "desc",
                "limit": 13,
                "file_type": "json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if len(obs) >= 13:
            latest = float(obs[0]["value"])
            year_ago = float(obs[12]["value"])
            return round((latest - year_ago) / year_ago * 100, 2)
    except Exception as exc:
        log.warning("CPI FRED fetch failed: %s", exc)
    return None


async def fetch_macro_context(include_geopolitical: bool = False) -> str:
    """Returns a formatted macro context string for injection into the Claude prompt.

    include_geopolitical appends a neutral macro/geopolitical headline brief (one
    live web-search call). It is OFF by default and enabled ONLY by the monthly
    Kimi Manager rebalance — the weekly Inspection deliberately stays holding-
    specific to avoid news-driven whiplash.
    """
    loop = asyncio.get_running_loop()
    fetches = [
        loop.run_in_executor(None, _fetch_vix_yield_sync),
        loop.run_in_executor(None, _fetch_cpi_sync),
    ]
    if include_geopolitical:
        fetches.append(loop.run_in_executor(None, _fetch_geopolitical_brief_sync))
    results = await asyncio.gather(*fetches)
    mkt, cpi = results[0], results[1]
    geo_brief = results[2] if include_geopolitical else None

    lines = ["Macro Context:"]
    if "vix" in mkt:
        vix = mkt["vix"]
        vix_label = "low" if vix < 18 else ("elevated" if vix < 25 else "HIGH — risk-off conditions")
        lines.append(f"  VIX: {vix:.1f} ({vix_label})")
    if "ten_year_yield" in mkt:
        lines.append(f"  10Y Treasury Yield: {mkt['ten_year_yield']:.2f}%")
    if cpi is not None:
        lines.append(f"  CPI YoY: {cpi:+.1f}%")
    else:
        lines.append("  CPI: unavailable (set FRED_API_KEY env var for live data)")

    if include_geopolitical:
        lines.append("")
        lines.append(
            "Macro/Geopolitical Backdrop (weigh relevance to each holding on its merits; "
            "this is situational context, not a signal to default to risk-off):"
        )
        lines.append(geo_brief if geo_brief else "  unavailable this run")

    return "\n".join(lines)
