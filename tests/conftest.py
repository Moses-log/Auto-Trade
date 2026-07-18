"""
conftest.py — Shared pytest fixtures and environment setup.

Sets required env vars before any app module is imported so that the
module-level `settings = Settings()` in app/config.py does not fail
when no .env file is present during testing.
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

# Set these before any app module is imported.  Using setdefault means a real
# .env file (if present) takes precedence because pydantic-settings reads it
# before the module-level singleton is constructed.
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


@pytest.fixture(autouse=True)
def isolate_idempotency_store(tmp_path):
    """Redirect the idempotency store to a fresh temp file for every test."""
    with patch("app.idempotency._FILE", tmp_path / "idempotency.json"):
        yield


@pytest.fixture(autouse=True)
def stub_inspection_spy_price():
    """The weekly inspection fetches a live SPY price for its context header and
    benchmark. Stub it so tests never hit yfinance/network. No-ops cleanly if the
    module isn't imported by a given test."""
    try:
        with patch("app.claude_inspection._fetch_spy_price", return_value=450.0):
            yield
    except (ImportError, AttributeError):
        yield
