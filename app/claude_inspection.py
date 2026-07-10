"""
claude_inspection.py — Weekly Kimi Inspection: a lightweight, holdings-only
review that runs on the first trading day of the week (skipped when it
coincides with the monthly rebalance), with authority to SELL, TRIM, or
DOUBLE_DOWN — never BUY. See docs/superpowers/specs/2026-07-09-kimi-inspection-design.md.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

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


async def run_weekly_inspection() -> None:
    """Placeholder — replaced with the real implementation in Task 8/9.

    Exists now so app/scheduler.py (Task 4) can import this name at module
    level; @patch("app.scheduler.run_weekly_inspection", ...) requires the
    attribute to already exist (patch's default create=False), which in turn
    requires app.claude_inspection.run_weekly_inspection to exist. Task 8
    replaces this entire function body — not append a second definition.
    """
    raise NotImplementedError("run_weekly_inspection is implemented in Task 8/9")


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
