"""
conftest.py — Shared pytest fixtures and environment setup.

Sets required env vars before any app module is imported so that the
module-level `settings = Settings()` in app/config.py does not fail
when no .env file is present during testing.
"""

import os

# Set these before any app module is imported.  Using setdefault means a real
# .env file (if present) takes precedence because pydantic-settings reads it
# before the module-level singleton is constructed.
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
