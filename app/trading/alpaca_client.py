"""
alpaca_client.py — Thin wrapper around alpaca-py with retry logic.

Responsibilities:
  - Build and cache a single TradingClient instance.
  - Wrap every Alpaca call in tenacity retry with exponential back-off so
    transient 5xx / rate-limit errors don't drop orders.
  - Expose the handful of operations order_logic.py needs.

We deliberately keep this module narrow — order-routing logic lives in
order_logic.py, not here.
"""

import logging
import math
from typing import Optional

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    ClosePositionRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Position, Order
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

from app.config import settings

log = logging.getLogger(__name__)

# ── Client singletons ─────────────────────────────────────────────────────────

_trading_client: Optional[TradingClient] = None
_data_client: Optional[StockHistoricalDataClient] = None


def get_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        _trading_client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=_is_paper(),
        )
        log.info(
            "Alpaca trading client initialised",
            extra={"paper": _is_paper(), "base_url": settings.alpaca_base_url},
        )
    return _trading_client


def get_data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
        )
        log.info("Alpaca data client initialised")
    return _data_client


def _is_paper() -> bool:
    return "paper" in settings.alpaca_base_url.lower()


# ── Retry decorator ───────────────────────────────────────────────────────────
# Retries up to 3 times on APIError (covers 429 / 5xx).
# Backs off 1s → 2s → 4s between attempts.

_retry = retry(
    retry=retry_if_exception_type(APIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


# ── Public helpers ────────────────────────────────────────────────────────────

@_retry
def get_account():
    """
    Return the Alpaca Account object.
    Used by order_logic.py to read buying_power for Kimi DD sizing.
    """
    account = get_client().get_account()
    log.debug(
        "Account fetched",
        extra={
            "equity":        str(account.equity),
            "buying_power":  str(account.buying_power),
            "cash":          str(account.cash),
        },
    )
    return account


@_retry
def get_latest_price(ticker: str) -> Optional[float]:
    """
    Return the latest trade price for ticker.
    Used as a fallback when price is not included in the alert payload.
    """
    try:
        req    = StockLatestTradeRequest(symbol_or_symbols=ticker)
        trades = get_data_client().get_stock_latest_trade(req)
        price  = float(trades[ticker].price)
        log.debug("Latest price fetched", extra={"ticker": ticker, "price": price})
        return price
    except Exception as exc:
        log.warning(
            "Could not fetch latest price",
            extra={"ticker": ticker, "error": str(exc)},
        )
        return None


@_retry
def get_position(ticker: str) -> Optional[Position]:
    """
    Return the open Position for *ticker*, or None if flat.
    Alpaca raises a 404-style APIError when there is no open position.
    """
    try:
        return get_client().get_open_position(ticker)
    except APIError as exc:
        if "position does not exist" in str(exc).lower() or "40410000" in str(exc):
            return None
        raise


@_retry
def place_market_order(
    ticker: str,
    side: OrderSide,
    qty: float,
) -> Order:
    """
    Submit a market order.

    qty is rounded to a whole number unless allow_fractional_shares is True.
    Raises ValueError if qty rounds to 0.
    """
    qty = _sanitise_qty(qty)

    req = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )

    log.info(
        "Submitting market order",
        extra={"ticker": ticker, "side": side.value, "qty": qty},
    )
    order = get_client().submit_order(req)
    log.info(
        "Order accepted",
        extra={
            "order_id": str(order.id),
            "ticker":   ticker,
            "side":     side.value,
            "qty":      qty,
            "status":   order.status,
        },
    )
    return order


@_retry
def close_position(ticker: str) -> Optional[Order]:
    """
    Fully close any open position for *ticker*.
    Returns None (not an error) if no position exists.
    """
    position = get_position(ticker)
    if position is None:
        log.info("No open position to close", extra={"ticker": ticker})
        return None

    log.info(
        "Closing position",
        extra={"ticker": ticker, "qty": position.qty, "side": position.side},
    )
    order = get_client().close_position(ticker)
    log.info(
        "Close-position order accepted",
        extra={"order_id": str(order.id), "ticker": ticker},
    )
    return order


# ── Internal helpers ──────────────────────────────────────────────────────────

def _sanitise_qty(qty: float) -> float:
    if not settings.allow_fractional_shares:
        qty = math.floor(qty)
    if qty <= 0:
        raise ValueError(
            f"Order quantity resolved to {qty} — must be > 0. "
            "Check 'contracts' in the TradingView alert and the "
            "ALLOW_FRACTIONAL_SHARES setting."
        )
    return qty
