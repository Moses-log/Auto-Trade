"""Tests for macro_context — focus on the rebalance-only geopolitical brief.

The brief must (1) stay OFF by default (weekly Inspection never sees it),
(2) render under a neutral, non-risk-off framing when enabled, and
(3) degrade gracefully to an 'unavailable' line when the fetch yields nothing.
"""
import os
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from unittest.mock import patch

import pytest

from app import macro_context


def _patch_market():
    """VIX/10Y present, CPI absent — a deterministic base context."""
    return patch.multiple(
        "app.macro_context",
        _fetch_vix_yield_sync=lambda: {"vix": 15.0, "ten_year_yield": 4.25},
        _fetch_cpi_sync=lambda: None,
    )


@pytest.mark.asyncio
async def test_default_excludes_geopolitical_backdrop():
    """Backward compatible: no flag => no backdrop section at all."""
    with _patch_market():
        text = await macro_context.fetch_macro_context()
    assert "VIX: 15.0" in text
    assert "Backdrop" not in text  # Inspection path must never receive it


@pytest.mark.asyncio
async def test_geopolitical_brief_included_with_neutral_framing():
    brief = "- Conflict A escalates in region X.\n- Central bank Y holds rates."
    with _patch_market(), patch(
        "app.macro_context._fetch_geopolitical_brief_sync", return_value=brief
    ):
        text = await macro_context.fetch_macro_context(include_geopolitical=True)
    assert "Macro/Geopolitical Backdrop" in text
    assert "do not" not in text.lower() or "risk-off" in text.lower()
    assert "default to risk-off" in text  # neutral anti-anchoring instruction present
    assert brief in text


@pytest.mark.asyncio
async def test_geopolitical_brief_degrades_gracefully():
    """A None brief (no key / failed call) must not crash and must be labeled."""
    with _patch_market(), patch(
        "app.macro_context._fetch_geopolitical_brief_sync", return_value=None
    ):
        text = await macro_context.fetch_macro_context(include_geopolitical=True)
    assert "Macro/Geopolitical Backdrop" in text
    assert "unavailable this run" in text


def test_brief_fetch_returns_none_without_api_key():
    with patch.object(macro_context.settings, "anthropic_api_key", None):
        assert macro_context._fetch_geopolitical_brief_sync() is None
