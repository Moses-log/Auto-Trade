"""
config.py — Application settings loaded from environment variables.

All sensitive values (API keys, secrets) must be set in a .env file
or as real environment variables. Never hard-code them here.
"""

from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Alpaca ────────────────────────────────────────────────────────────────
    alpaca_api_key: str
    alpaca_secret_key: str
    # Paper trading endpoint by default. Switch to https://api.alpaca.markets
    # for live trading only after thorough testing.
    alpaca_base_url: str = "https://paper-api.alpaca.markets/v2"

    # ── Webhook security ──────────────────────────────────────────────────────
    # Must match the "secret" field TradingView sends in every alert payload.
    webhook_secret: str

    # ── Server ────────────────────────────────────────────────────────────────
    port: int = 8000

    # ── Idempotency ───────────────────────────────────────────────────────────
    # How long (seconds) to remember a processed alert_id to block duplicates.
    idempotency_ttl: int = 300  # 5 minutes

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Optional notifications (leave blank to disable) ───────────────────────
    discord_webhook_url: Optional[str] = None
    discord_investors_webhook_url: Optional[str] = None
    github_token: Optional[str] = None
    github_repo: str = "Moses-log/Auto-Trade"
    discord_trades_webhook_url: Optional[str] = None

    # ── Discord slash commands ─────────────────────────────────────────────────
    discord_app_public_key: Optional[str] = None
    discord_app_id: Optional[str] = None
    discord_your_user_id: Optional[str] = None

    # ── Withdrawal approval delay ─────────────────────────────────────────────
    # Hours a /withdraw request waits before it actually executes. Gives the
    # operator a window to /cancel-withdrawal a request made by a compromised
    # credential before investor funds are actually moved in the ledger.
    withdrawal_delay_hours: int = 24

    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # ── Order defaults ────────────────────────────────────────────────────────
    # Set to True only if your Alpaca account has fractional-share trading
    # enabled AND the symbol supports it.
    allow_fractional_shares: bool = False

    # ── Storage backend ───────────────────────────────────────────────────────
    # When True, the investor ledger (investors / pending withdrawals / audit /
    # rh deposits) is stored in SQLite (/data/kimi.db) and the JSON files become
    # derived read-only exports. Default False = pure-JSON behavior (unchanged).
    use_sqlite: bool = False

    # ── Robinhood ─────────────────────────────────────────────────────────────
    rh_username: Optional[str] = None
    rh_password: Optional[str] = None
    rh_leverage_factor: float = 0.3
    rh_enabled: bool = True
    rh_discord_webhook_url: Optional[str] = None
    rh_session_webhook_url: Optional[str] = None
    rh_pnl_webhook_url: Optional[str] = None
    # Set to the account_number of the Robinhood account to trade on.
    # Leave blank to use the default (primary) account.
    rh_account_number: Optional[str] = None

    # ── Tax report channels ───────────────────────────────────────────────────
    alpaca_tax_webhook_url: Optional[str] = None
    rh_tax_webhook_url: Optional[str] = None

    # ── Claude Autopilot Portfolio ────────────────────────────────────────────
    # Fraction of RH buying power to use for each Claude portfolio trade.
    claude_leverage_factor: float = 0.05
    claude_portfolio_webhook_url: Optional[str] = None

    # ── Claude Portfolio Manager (autonomous monthly rebalancer) ──────────────
    anthropic_api_key: Optional[str] = None
    claude_manager_webhook_url: Optional[str] = None
    fred_api_key: Optional[str] = None

    # ── Portfolio Snapshot (RH pie chart + valuation) ─────────────────────────
    portfolio_snapshot_webhook_url: Optional[str] = None

    # ── Signal Subscribers (paid Discord feed — sanitized BUY/SELL alerts) ────
    signal_subscribers_webhook_url: Optional[str] = None

    # ── Claude Manager Subscribers (paid Discord feed — Claude stock callouts) ─
    claude_subscribers_webhook_url: Optional[str] = None

    # ── Whop webhook (early-access spot counter) ──────────────────────────────
    whop_webhook_secret: Optional[str] = None

    # ── GitHub Gist backup (second offsite copy of /data/ files) ─────────────
    # PAT with "gist" scope — github.com/settings/tokens
    github_gist_token: Optional[str] = None
    # ID of the private Gist to update (visible in the Gist URL)
    github_gist_id: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


# Single shared instance — import this everywhere else.
settings = Settings()
