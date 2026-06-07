import os
os.environ.setdefault("ALPACA_API_KEY",    "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET",    "MY_SHARED_SECRET")

from app.config import settings

def test_rh_defaults():
    assert settings.rh_enabled is True
    assert settings.rh_leverage_factor == 0.3
    assert settings.rh_username is None
    assert settings.rh_password is None
    assert settings.rh_discord_webhook_url is None
