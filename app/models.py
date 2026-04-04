"""
models.py — Pydantic models for incoming TradingView webhook payloads.

TradingView sends JSON in the alert message body. The model below mirrors
the template you paste into TradingView's "Message" field. All fields that
TradingView might not fill are Optional so validation never hard-crashes on
a partially-filled alert.
"""

from typing import Optional
from pydantic import BaseModel, field_validator
from enum import Enum


class TradingAction(str, Enum):
    """Normalised set of actions this system understands."""
    BUY             = "buy"
    SELL            = "sell"
    CLOSE_LONG      = "close_long"
    CLOSE_SHORT     = "close_short"
    REVERSE_TO_LONG = "reverse_to_long"
    REVERSE_TO_SHORT= "reverse_to_short"


class AlertPayload(BaseModel):
    """
    Mirrors the TradingView alert message template exactly.
    Extra fields are ignored (model_config extra='ignore').
    """
    # Auth — must match WEBHOOK_SECRET env var
    secret: str

    # Symbol, e.g. "AAPL", "SPY", "TSLA"
    ticker: str

    # One of the TradingAction enum values (case-insensitive)
    action: TradingAction

    # Number of shares/contracts from {{strategy.order.contracts}}
    # Can arrive as a float string like "10.0" or an integer
    contracts: Optional[float] = None

    # Current bar close price — informational, not used for order pricing
    price: Optional[float] = None

    # TradingView strategy order ID — used as idempotency key
    order_id: Optional[str] = None

    # Strategy position context
    market_position:          Optional[str]  = None  # "long" | "short" | "flat"
    market_position_size:     Optional[float] = None
    prev_market_position:     Optional[str]  = None
    prev_market_position_size:Optional[float] = None

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
        """Accept mixed-case actions."""
        return v.strip().lower()

    @field_validator("contracts", mode="before")
    @classmethod
    def parse_contracts(cls, v):
        if v is None or v == "" or v == "NaN":
            return None
        return float(v)

    model_config = {"extra": "ignore"}
