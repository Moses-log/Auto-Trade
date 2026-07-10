"""
claude_inspection.py — Weekly Kimi Inspection: a lightweight, holdings-only
review that runs on the first trading day of the week (skipped when it
coincides with the monthly rebalance), with authority to SELL, TRIM, or
DOUBLE_DOWN — never BUY. See docs/superpowers/specs/2026-07-09-kimi-inspection-design.md.
"""

from __future__ import annotations

import asyncio
import json
import json as _json
import logging
import os
from datetime import datetime

import pytz

from app.claude_manager import (
    _embed, _timestamp, _fetch_yf_data, _fetch_technical_data,
    _CLR_ORANGE, _CLR_GREEN, _CLR_GRAY, _LOG_PATH,
)
from app.claude_manager import _trade_embed, _field, _CLR_RED
from app.claude_portfolio import open_position, close_position, trim_position, get_record
from app.notifications import notify_claude_manager_embed, notify_claude_signal_feed
from app.trading.robinhood_client import rh_client

log = logging.getLogger(__name__)

_CT = pytz.timezone("America/Chicago")
_INSPECTION_LOG_PATH = os.getenv("CLAUDE_INSPECTION_LOG_PATH", "/data/claude_inspection_log.json")


def _append_inspection_log(entry: dict) -> None:
    try:
        try:
            with open(_INSPECTION_LOG_PATH) as f:
                records = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            records = []
        records.append(entry)
        if len(records) > 36:          # cap at ~3 years of weekly logs (52/yr, generous)
            records = records[-36:]
        with open(_INSPECTION_LOG_PATH, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as exc:
        log.warning("Failed to write inspection log: %s", exc)


def _load_recent_inspection_entries(limit: int = 5) -> list[dict]:
    try:
        with open(_INSPECTION_LOG_PATH) as f:
            records = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return records[-limit:]


def _load_recent_rebalance_records(limit: int = 1) -> list:
    try:
        with open(_LOG_PATH) as f:
            records = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return []
    return records[-limit:]


async def run_weekly_inspection() -> None:
    """Weekly holdings-only review. Never opens a new position — see
    docs/superpowers/specs/2026-07-09-kimi-inspection-design.md."""
    log_entry: dict = {
        "timestamp": datetime.now(_CT).isoformat(),
        "status": "started",
        "holdings_reviewed": [],
        "trades_executed": [],
        "trades_skipped": [],
        "notes": {},
    }

    if not rh_client.available:
        log.warning("RH session unavailable — skipping weekly inspection")
        log_entry["status"] = "skipped_rh_unavailable"
        _append_inspection_log(log_entry)
        await notify_claude_manager_embed(_embed(
            "⚠️ INSPECTION SKIPPED — Robinhood session is offline",
            _CLR_ORANGE, footer=_timestamp(),
        ))
        return

    try:
        positions = await rh_client.get_all_positions_async()
        if not positions:
            log_entry["status"] = "no_holdings"
            _append_inspection_log(log_entry)
            await notify_claude_manager_embed(_embed(
                "🔍 KIMI INSPECTION — no current holdings to review",
                _CLR_GRAY, footer=_timestamp(),
            ))
            return

        loop = asyncio.get_running_loop()
        yf_tasks = [loop.run_in_executor(None, _fetch_yf_data, pos["symbol"]) for pos in positions]
        fv_tasks = [loop.run_in_executor(None, _fetch_technical_data, pos["symbol"]) for pos in positions]
        yf_results, fv_results = await asyncio.gather(
            asyncio.gather(*yf_tasks, return_exceptions=True),
            asyncio.gather(*fv_tasks, return_exceptions=True),
        )

        enriched = []
        for pos, yf_data, fv_data in zip(positions, yf_results, fv_results):
            yf_data = yf_data if isinstance(yf_data, dict) else {"ticker": pos["symbol"]}
            fv_data = fv_data if isinstance(fv_data, dict) else {}
            enriched.append({
                **yf_data, **fv_data,
                "qty": pos["qty"],
                "avg_entry_price": pos["avg_entry_price"],
                "current_price": pos.get("current_price"),
                "unrealized_pnl_pct": round(pos.get("unrealized_plpc", 0), 2),
            })
        log_entry["holdings_reviewed"] = [e["ticker"] for e in enriched if e.get("ticker")]

        rebalance_records = _load_recent_rebalance_records(limit=1)
        inspection_records = _load_recent_inspection_entries(limit=5)
        thesis_map = _build_prior_thesis_map(rebalance_records, inspection_records)

        holdings_json = _json.dumps(enriched, indent=2)
        thesis_lines = "\n\n".join(
            f"### {ticker}\n{thesis_map.get(ticker, 'No prior thesis on record — treat conservatively.')}"
            for ticker in log_entry["holdings_reviewed"]
        )
        prompt = (
            f"Weekly Inspection — review current holdings for anything material since the last check-in.\n\n"
            f"Current Holdings:\n{holdings_json}\n\n"
            f"Most Recent Thesis Per Ticker:\n{thesis_lines}\n\n"
            f"For each holding, decide HOLD / SELL / TRIM / DOUBLE_DOWN per the rules in your system prompt. "
            f"End with the required JSON block."
        )

        try:
            response_text = await loop.run_in_executor(None, _call_claude_inspection_sync, prompt)
        except Exception as exc:
            log.error("Inspection Claude API call failed: %s", exc)
            log_entry["status"] = "failed_claude_api"
            _append_inspection_log(log_entry)
            await notify_claude_manager_embed(_embed(
                "❌ INSPECTION FAILED — Anthropic API error",
                _CLR_ORANGE, description=str(exc), footer=_timestamp(),
            ))
            return

        trade_block = _parse_inspection_trade_block(response_text)

        if trade_block is None:
            log_entry["status"] = "failed_parse_or_buy_rejected"
            _append_inspection_log(log_entry)
            await notify_claude_manager_embed(_embed(
                "⚠️ INSPECTION — could not parse response (or a disallowed BUY was proposed)",
                _CLR_ORANGE,
                description="No trades executed this week — see logs for the raw response.",
                footer=_timestamp(),
            ))
            return

        if trade_block.get("no_changes") or not [
            t for t in trade_block.get("trades", []) if t.get("action") != "HOLD"
        ]:
            log_entry["status"] = "no_changes"
            _append_inspection_log(log_entry)
            await notify_claude_manager_embed(_embed(
                "🔍 KIMI INSPECTION — no material changes this week",
                _CLR_GREEN,
                description=f"Reviewed {len(log_entry['holdings_reviewed'])} holding(s); no action needed.",
                footer=_timestamp(),
            ))
            return

        pending_trades = [t for t in trade_block["trades"] if t.get("action") != "HOLD"]
        position_by_ticker = {p["symbol"]: p for p in positions}
        portfolio_value = sum(p["qty"] * p.get("current_price", 0) for p in positions)

        await notify_claude_manager_embed(_embed(
            f"🔍 KIMI INSPECTION — {len(pending_trades)} action(s) this week",
            _CLR_ORANGE, footer=_timestamp(),
        ))

        for trade in pending_trades:
            ticker = trade["ticker"].upper()
            action = trade["action"]
            reasoning = trade.get("reasoning", "")
            log_entry["notes"][ticker] = reasoning or f"{action} — see full analysis in Discord."
            pos = position_by_ticker.get(ticker)

            if pos is None:
                log_entry["trades_skipped"].append({"action": action, "ticker": ticker, "reason": "no position"})
                continue

            if action == "SELL":
                result = await rh_client.close_ticker_async(ticker)
                if result.get("status") != "ok" or not result.get("qty"):
                    reason = result.get("reason", result.get("note", "unknown"))
                    log_entry["trades_skipped"].append({"action": "SELL", "ticker": ticker, "reason": reason})
                    await asyncio.sleep(0.8)
                    await notify_claude_manager_embed(_embed(
                        f"❌ KIMI INSPECTION SELL — {ticker} FAILED",
                        _CLR_RED, description=reason, footer=_timestamp(),
                    ))
                    continue
                qty = result["qty"]
                fill = result.get("fill_price") or result.get("price_est")
                _, dollar_pnl, pct_pnl = close_position(ticker, fill or 0.0)
                if dollar_pnl is not None:
                    from app.rh_trade_record import record_rh_trade
                    await record_rh_trade(dollar_pnl >= 0, ticker, dollar_pnl)
                wins, losses = get_record()
                log_entry["trades_executed"].append({
                    "action": "SELL", "ticker": ticker, "qty": qty,
                    "fill_price": fill, "dollar_pnl": dollar_pnl, "reasoning": reasoning,
                })
                pnl_str = (f"+${dollar_pnl:,.2f} ({pct_pnl:+.2f}%)" if dollar_pnl is not None else "—")
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_trade_embed(
                    "SELL", ticker,
                    [_field("Qty", f"{qty:g} shares @ ${fill or 0:,.2f}"),
                     _field("Record", f"{wins}W — {losses}L"),
                     _field("Reasoning", reasoning or "—", inline=False),
                     _field("P&L", pnl_str, inline=False)],
                    _timestamp(),
                ))
                await notify_claude_signal_feed(
                    f"🔴 **KIMI INSPECTION SELL — {ticker}**\n{reasoning or 'See analysis.'}\n"
                    f"@ ${fill or 0:,.2f}\n{_timestamp()}"
                )

            elif action == "TRIM":
                target_wt = trade.get("target_weight_pct", 5)
                target_value = portfolio_value * target_wt / 100
                current_qty = pos["qty"]
                current_price = pos.get("current_price", 0)
                current_value = current_qty * current_price
                if current_qty < 1.0 or target_value >= current_value * 0.95:
                    reason = "fractional position" if current_qty < 1.0 else "already at target"
                    log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": reason})
                    continue
                sell_qty = round((current_value - target_value) / current_price, 6) if current_price > 0 else 0.0
                if sell_qty <= 0:
                    log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": "sell qty <= 0"})
                    continue
                result = await rh_client.sell_shares_async(ticker, sell_qty)
                if result.get("status") != "ok":
                    reason = result.get("reason", "unknown")
                    log_entry["trades_skipped"].append({"action": "TRIM", "ticker": ticker, "reason": reason})
                    await asyncio.sleep(0.8)
                    await notify_claude_manager_embed(_embed(
                        f"❌ KIMI INSPECTION TRIM — {ticker} FAILED",
                        _CLR_RED, description=reason, footer=_timestamp(),
                    ))
                    continue
                qty_sold = result.get("qty", sell_qty)
                fill = result.get("fill_price") or result.get("price_est")
                _, dollar_pnl, pct_pnl = trim_position(ticker, qty_sold, fill or 0.0)
                if dollar_pnl is not None:
                    from app.rh_trade_record import record_rh_trade
                    await record_rh_trade(dollar_pnl >= 0, ticker, dollar_pnl)
                wins, losses = get_record()
                log_entry["trades_executed"].append({
                    "action": "TRIM", "ticker": ticker, "qty": qty_sold,
                    "fill_price": fill, "dollar_pnl": dollar_pnl,
                    "target_weight_pct": target_wt, "reasoning": reasoning,
                })
                pnl_str = (f"+${dollar_pnl:,.2f} ({pct_pnl:+.2f}%)" if dollar_pnl is not None else "—")
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_trade_embed(
                    "TRIM", ticker,
                    [_field("Sold", f"{qty_sold:g} shares @ ${fill or 0:,.2f}"),
                     _field("→ Target", f"{target_wt}%"),
                     _field("Reasoning", reasoning or "—", inline=False),
                     _field("P&L", pnl_str, inline=False)],
                    _timestamp(),
                ))
                await notify_claude_signal_feed(
                    f"✂️ **KIMI INSPECTION TRIM — {ticker}**\n{reasoning or 'See analysis.'}\n"
                    f"→ target {target_wt}% · {_timestamp()}"
                )

            elif action == "DOUBLE_DOWN":
                target_wt = trade.get("target_weight_pct", 10)
                target_dollars = portfolio_value * target_wt / 100
                current_val = pos["qty"] * pos.get("current_price", 0)
                buying_power = await rh_client.get_buying_power_async() or 0.0
                delta_dollars = max(0.0, target_dollars - current_val)
                invest_dollars = min(delta_dollars, buying_power * 0.95)
                if invest_dollars < 1:
                    log_entry["trades_skipped"].append({
                        "action": "DOUBLE_DOWN", "ticker": ticker,
                        "reason": f"needed ${delta_dollars:,.0f}, only ${buying_power:,.0f} available",
                    })
                    continue
                result = await rh_client.buy_dollars_async(ticker, invest_dollars)
                if result.get("status") != "ok":
                    reason = result.get("reason", "unknown")
                    log_entry["trades_skipped"].append({"action": "DOUBLE_DOWN", "ticker": ticker, "reason": reason})
                    await asyncio.sleep(0.8)
                    await notify_claude_manager_embed(_embed(
                        f"❌ KIMI INSPECTION DOUBLE_DOWN — {ticker} FAILED",
                        _CLR_RED, description=reason, footer=_timestamp(),
                    ))
                    continue
                qty = result.get("qty", 0)
                fill = result.get("fill_price") or result.get("price_est", 0)
                open_position(ticker, qty, fill or 0.0)
                log_entry["trades_executed"].append({
                    "action": "DOUBLE_DOWN", "ticker": ticker, "qty": qty,
                    "fill_price": fill, "dollars_invested": invest_dollars,
                    "target_weight_pct": target_wt, "reasoning": reasoning,
                })
                await asyncio.sleep(0.8)
                await notify_claude_manager_embed(_trade_embed(
                    "DOUBLE_DOWN", ticker,
                    [_field("Qty", f"{qty:g} shares @ ${fill or 0:,.2f}"),
                     _field("Target Weight", f"{target_wt}%"),
                     _field("Reasoning", reasoning or "—", inline=False)],
                    _timestamp(),
                ))
                await notify_claude_signal_feed(
                    f"🔥 **KIMI INSPECTION DOUBLE_DOWN — {ticker}**\n{reasoning or 'See analysis.'}\n"
                    f"Target: {target_wt}% · {_timestamp()}"
                )

        log_entry["status"] = "completed"
        _append_inspection_log(log_entry)

        executed = len(log_entry["trades_executed"])
        skipped = len(log_entry["trades_skipped"])
        await asyncio.sleep(0.8)
        await notify_claude_manager_embed(_embed(
            "✅ KIMI INSPECTION COMPLETE",
            _CLR_GREEN,
            description=f"{executed} trade(s) executed"
                        + (f", {skipped} skipped" if skipped else ""),
            footer=_timestamp(),
        ))

    except Exception as exc:
        log.error("Unhandled error in run_weekly_inspection: %s", exc)
        log_entry["status"] = "failed_unexpected_error"
        _append_inspection_log(log_entry)
        await notify_claude_manager_embed(_embed(
            "❌ INSPECTION FAILED — unexpected error",
            _CLR_ORANGE, description=str(exc), footer=_timestamp(),
        ))


import httpx

from app.claude_manager import _parse_trade_block, _DIVIDER, _section_ticker
from app.config import settings

_INSPECTION_WEB_SEARCH_TOOL: dict = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 15,
}

_INSPECTION_SYSTEM_PROMPT = """You are Kimi Inspection, a weekly holdings-only check-in that runs \
between Kimi Portfolio Manager's monthly rebalances.

Your job is narrower than a full rebalance: for each current holding, decide whether anything \
material has happened in the last 7 days that changes the existing thesis. You are NOT re-deriving \
each thesis from scratch — you are given the most recent thesis for each ticker and asked whether \
it still holds.

DEFAULT TO HOLD. Only recommend action (SELL, TRIM, or DOUBLE_DOWN) when there is a specific, \
nameable trigger:
- An earnings surprise (beat or miss) since the last review
- A guidance change (raised or cut)
- Major company-specific news (management change, regulatory action, product failure, M&A)
- A macro/sector shock clearly tied to this specific name
- A meaningful technical breakdown (a major support level broken with volume, a fresh death cross)

Routine day-to-day price noise is NOT a trigger. If nothing material happened for a holding, the \
correct action is HOLD — do not manufacture a reason to trade.

HARD RULE: You may never propose BUY. You only act on tickers already held. New positions are \
opened exclusively by the monthly rebalance's candidate screening — that is out of scope here.

Position-sizing constraints (same as the monthly rebalance): maximum position size 25%, no single \
sector above 50%, and a DOUBLE_DOWN that would push a position above 10% requires you to explicitly \
state why the existing bear case is still resolved. SPY is permanently excluded — never mention it.

REQUIRED OUTPUT FORMAT: end your response with a JSON block in exactly this format:

```json
{
  "no_changes": false,
  "trades": [
    {"action": "HOLD", "ticker": "MSFT"},
    {"action": "SELL", "ticker": "NOW"},
    {"action": "TRIM", "ticker": "NVDA", "target_weight_pct": 8},
    {"action": "DOUBLE_DOWN", "ticker": "META", "target_weight_pct": 22}
  ]
}
```

Rules for the JSON block:
- Set "no_changes": true if no holding needs any action this week.
- action must be exactly "HOLD", "SELL", "TRIM", or "DOUBLE_DOWN" — never "BUY".
- Every current holding must appear exactly once in "trades".
- target_weight_pct is required for TRIM and DOUBLE_DOWN; omit for SELL and HOLD.
- Do not include markdown, comments, or extra fields in the JSON block."""


def _call_claude_inspection_sync(user_message: str) -> str:
    """Agentic loop with live web search, sized for a weekly holdings-only check.

    Same shape as claude_manager._call_claude_sync but with a smaller turn cap
    (30 vs 80) and a smaller web-search budget (15 vs 30 uses), since Inspection
    only does a delta-check against the last known thesis, not a full rebuild.
    """
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05",
        "content-type": "application/json",
    }
    messages: list[dict] = [{"role": "user", "content": user_message}]
    for _turn in range(30):
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": "claude-opus-4-8",
                "max_tokens": 8000,
                "system": _INSPECTION_SYSTEM_PROMPT,
                "messages": messages,
                "tools": [_INSPECTION_WEB_SEARCH_TOOL],
            },
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        content: list = data["content"]
        stop_reason: str = data.get("stop_reason", "end_turn")
        messages.append({"role": "assistant", "content": content})
        if stop_reason == "end_turn":
            return "\n".join(b["text"] for b in content if b.get("type") == "text")
        if stop_reason == "max_tokens":
            log.error("Inspection call hit max_tokens limit — response may be truncated")
            return "\n".join(b["text"] for b in content if b.get("type") == "text")
        if stop_reason == "tool_use":
            resolved_ids = {b["tool_use_id"] for b in content if b.get("tool_use_id")}
            pending = [b for b in content if b.get("type") == "tool_use" and b.get("id") not in resolved_ids]
            if pending:
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": b["id"], "content": ""} for b in pending],
                })
            continue
        break
    log.warning("Inspection agentic loop hit 30-turn safety cap — returning last assistant turn only")
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            texts = [
                b["text"] for b in (msg["content"] if isinstance(msg["content"], list) else [])
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


def _parse_inspection_trade_block(text: str) -> "dict | None":
    """Parse the trade block and reject it wholesale if it contains a BUY.

    Inspection must never open a new position — this is enforced here, not
    just in the prompt. A BUY appearing anywhere in the block indicates a
    prompt/constraint failure worth surfacing loudly (the caller logs and
    skips execution for the whole run) rather than silently dropping just
    the BUY and executing the rest.
    """
    block = _parse_trade_block(text)
    if block is None:
        return None
    trades = block.get("trades", [])
    if any(t.get("action") == "BUY" for t in trades):
        log.error("Inspection proposed a BUY action — rejecting entire trade block: %s", trades)
        return None
    return block


def _build_prior_thesis_map(rebalance_records: list, inspection_records: list) -> dict:
    """Build {ticker: most_recent_thesis_text}, sourced from the last rebalance's
    per-ticker research sections, then overlaid with any more recent Inspection
    notes (an Inspection that ran after the last rebalance has a fresher view)."""
    thesis_map: dict = {}

    if rebalance_records:
        last = rebalance_records[-1]
        analysis_body = last.get("analysis_body") or ""
        for section in analysis_body.split(_DIVIDER):
            section = section.strip()
            if not section:
                continue
            ticker = _section_ticker(section)
            if ticker:
                thesis_map[ticker] = section

    for entry in inspection_records:
        for ticker, note in (entry.get("notes") or {}).items():
            thesis_map[ticker.upper()] = note

    return thesis_map
