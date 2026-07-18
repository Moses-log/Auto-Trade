"""Prompt-caching helpers for the Anthropic Messages API agentic loops.

Within a single agentic loop the same large prefix — tools + system prompt +
the initial holdings/context, then the accumulating web-search results — is
re-sent on every turn. Marking it with `cache_control` lets turns after the
first read that prefix from cache (~0.1x input price) instead of paying full
price each time. Identical model, prompt, and output; purely cheaper + faster.

Two breakpoints are used (well under the 4-per-request maximum):
  1. A static one on the system block, which also caches the tools that render
     before it (render order is tools -> system -> messages).
  2. A rolling one on the last block of the newest message, so the growing
     conversation prefix caches incrementally. The prior rolling marker is
     stripped each turn so only one lives in the message list at a time.

Shared by the monthly rebalance (claude_manager), the weekly Inspection
(claude_inspection), and the geopolitical brief (macro_context).
"""
from __future__ import annotations

_EPHEMERAL = {"type": "ephemeral"}


def cached_system(text: str) -> list[dict]:
    """System prompt as a single cache_control-marked text block."""
    return [{"type": "text", "text": text, "cache_control": _EPHEMERAL}]


def apply_message_cache(messages: list[dict]) -> None:
    """Move the rolling cache breakpoint to the last block of the last message.

    Mutates `messages` in place. Strips any prior cache_control from message
    blocks first (so at most one rolling breakpoint exists), then marks the
    final block of the final message — converting a bare string message into a
    text block so it can carry the marker. cache_control is a caching hint only,
    so stripping/adding it never changes the prompt's meaning or the output.
    """
    if not messages:
        return
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content, "cache_control": _EPHEMERAL}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = _EPHEMERAL
