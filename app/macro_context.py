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


async def fetch_macro_context() -> str:
    """Returns a formatted macro context string for injection into the Claude prompt."""
    loop = asyncio.get_running_loop()
    mkt, cpi = await asyncio.gather(
        loop.run_in_executor(None, _fetch_vix_yield_sync),
        loop.run_in_executor(None, _fetch_cpi_sync),
    )
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
    return "\n".join(lines)
