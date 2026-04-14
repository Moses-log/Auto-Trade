"""
models.py — Pydantic models for incoming TradingView webhook payloads.
"""
from typing import Optional
from pydantic import BaseModel, field_validator
from enum import Enum


class TradingAction(str, Enum):
    """Normalised set of actions this system understands."""
    BUY              = "buy"
    SELL             = "sell"
    CLOSE_LONG       = "close_long"
    CLOSE_SHORT      = "close_short"
    REVERSE_TO_LONG  = "reverse_to_long"
    REVERSE_TO_SHORT = "reverse_to_short"
    # ── Kimi strategy actions ─────────────────────────────────────────────────
    BASE_ENTRY       = "base_entry"       # First entry — you place manually, bot ignores
    ADD_LEVERAGE     = "add_leverage"     # DD buy — bot calculates qty from Alpaca balance
    REMOVE_LEVERAGE  = "remove_leverage"  # DD sell — bot closes "Leverage" position
    STOP_LOSS        = "stop_loss"        # Full close — bot closes all positions


class AlertPayload(BaseModel):
    """
    Mirrors the TradingView alert message template exactly.
    Extra fields are ignored (model_config extra='ignore').
    """
    # Auth — must match WEBHOOK_SECRET env var
    secret: str

    # Symbol, e.g. "SPY"
    ticker: str

    # One of the TradingAction enum values (case-insensitive)
    action: TradingAction

    # Number of shares/contracts — used for legacy buy/sell actions
    # For Kimi actions (add_leverage etc.) qty is calculated live from Alpaca
    contracts: Optional[float] = None

    # Current bar close price — used for Kimi DD sizing
    price: Optional[float] = None

    # TradingView strategy order ID — used as idempotency key
    order_id: Optional[str] = None

    # Strategy position context
    market_position:           Optional[str]   = None
    market_position_size:      Optional[float] = None
    prev_market_position:      Optional[str]   = None
    prev_market_position_size: Optional[float] = None

    # ISO timestamp from {{timenow}}
    timestamp: Optional[str] = None

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("ticker", mode="before")
    @classmethod
    def clean_ticker(cls, v: str) -> str:
        """Strip exchange prefix like 'NASDAQ:AAPL' → 'AAPL'."""
        if ":" in v:
            v = v.split(":")[-1]
        return v.strip().upper()

    @field_validator("action", mode="before")
    @classmethod
    def normalise_action(cls, v: str) -> str:
        """Accept mixed-case actions and convert Kimi plain-English messages."""
        v = v.strip().lower()
        # Map Kimi alert() messages to enum values
        mapping = {
            "base entry":       "base_entry",
            "add leverage":     "add_leverage",
            "remove leverage":  "remove_leverage",
            "stop loss":        "stop_loss",
        }
        return mapping.get(v, v)

    @field_validator("contracts", mode="before")
    @classmethod
    def parse_contracts(cls, v):
        if v is None or v == "" or v == "NaN":
            return None
        return float(v)

    model_config = {"extra": "ignore"}
