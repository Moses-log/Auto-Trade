import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def test_hf_webhook_fields_default_none():
    from app.config import Settings
    s = Settings()
    assert s.alpaca_hf_trades_webhook_url is None
    assert s.alpaca_hf_recap_webhook_url is None


def test_hf_webhook_fields_read_env(monkeypatch):
    monkeypatch.setenv("ALPACA_HF_TRADES_WEBHOOK_URL", "https://x/trades")
    monkeypatch.setenv("ALPACA_HF_RECAP_WEBHOOK_URL", "https://x/recap")
    from app.config import Settings
    s = Settings()
    assert s.alpaca_hf_trades_webhook_url == "https://x/trades"
    assert s.alpaca_hf_recap_webhook_url == "https://x/recap"
