"""Tests for the prompt-caching helpers shared by the three agentic loops.

cache_control is a caching hint only — these tests pin the invariant that the
helper places exactly one rolling breakpoint on the newest turn (plus the
static system one added separately) and never leaves stale breakpoints behind,
so a request never exceeds the 4-breakpoint maximum.
"""
from app.anthropic_cache import apply_message_cache, cached_system

_EPHEMERAL = {"type": "ephemeral"}


def _cache_marked_blocks(messages):
    """All message content blocks carrying a cache_control marker."""
    marked = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            marked.extend(b for b in content if isinstance(b, dict) and "cache_control" in b)
    return marked


def test_cached_system_marks_single_block():
    payload = cached_system("SYSTEM PROMPT TEXT")
    assert payload == [
        {"type": "text", "text": "SYSTEM PROMPT TEXT", "cache_control": _EPHEMERAL}
    ]


def test_string_message_becomes_marked_text_block():
    messages = [{"role": "user", "content": "hello"}]
    apply_message_cache(messages)
    assert messages[0]["content"] == [
        {"type": "text", "text": "hello", "cache_control": _EPHEMERAL}
    ]


def test_marks_last_block_of_last_message():
    messages = [
        {"role": "user", "content": "kickoff"},
        {"role": "assistant", "content": [{"type": "text", "text": "thinking"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": ""},
            {"type": "tool_result", "tool_use_id": "b", "content": ""},
        ]},
    ]
    apply_message_cache(messages)
    marked = _cache_marked_blocks(messages)
    # Exactly one rolling breakpoint, on the final block of the final message.
    assert len(marked) == 1
    assert messages[-1]["content"][-1]["cache_control"] == _EPHEMERAL


def test_rolling_breakpoint_moves_and_never_accumulates():
    """Simulate an agentic loop: at most one message breakpoint ever exists."""
    messages = [{"role": "user", "content": "kickoff"}]
    apply_message_cache(messages)
    assert len(_cache_marked_blocks(messages)) == 1

    # Turn advances: assistant reply + new tool_result user turn appended.
    messages.append({"role": "assistant", "content": [{"type": "text", "text": "t1"}]})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": ""}]})
    apply_message_cache(messages)

    marked = _cache_marked_blocks(messages)
    assert len(marked) == 1  # old marker stripped, one new marker placed
    assert messages[-1]["content"][-1]["cache_control"] == _EPHEMERAL


def test_empty_messages_is_noop():
    messages = []
    apply_message_cache(messages)  # must not raise
    assert messages == []
