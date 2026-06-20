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
from datetime import date, datetime, timedelta
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
    GetPortfolioHistoryRequest,
    GetCalendarRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import QueryOrderStatus, OrderStatus as AlpacaOrderStatus
from alpaca.common.enums import Sort
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Position, Order
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

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
def get_portfolio_history(period: Optional[str] = None, timeframe: Optional[str] = None, start: Optional[datetime] = None):
    """Fetch portfolio equity history from Alpaca.

    Args:
        period:    Lookback window — "1D" for daily, "1W" for weekly. Omit when using start.
        timeframe: Data granularity — "1Min" for intraday, "1D" for daily bars.
        start:     Explicit start datetime (RFC3339/tz-aware). Use instead of period
                   to fetch history from a fixed date (e.g. fund inception or a
                   user-supplied custom date) through today.

    Returns:
        PortfolioHistory object with .equity list (float) and .timestamp list (int).
    """
    return get_client().get_portfolio_history(
        GetPortfolioHistoryRequest(period=period, timeframe=timeframe, start=start)
    )


@_retry
def get_spy_bars(start, end):
    """Fetch daily SPY bars between start and end.

    Args:
        start: datetime (ET timezone) — market open of the period start
        end:   datetime (ET timezone) — current time (4pm ET at report time)

    Returns:
        BarSet dict-like object. Access bars via bars["SPY"] → list of Bar.
        Each Bar has .open and .close float attributes.
    """
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
    )
    return get_data_client().get_stock_bars(req)


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


@_retry
def get_orders_filled_range(after: datetime, until: datetime) -> list:
    """Return up to 500 filled orders between after and until, sorted ascending."""
    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=after,
        until=until,
        limit=500,
        direction=Sort.ASC,
    )
    orders = get_client().get_orders(req) or []
    return [o for o in orders if o.status == AlpacaOrderStatus.FILLED]


@_retry
def get_all_spy_orders() -> list:
    """Return all closed SPY orders (filled), up to 500, sorted ascending by fill time."""
    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        limit=500,
    )
    orders = get_client().get_orders(req) or []
    return [o for o in orders if o.symbol == "SPY" and o.status == AlpacaOrderStatus.FILLED]


@_retry
def get_all_positions() -> list:
    """Return all open positions as a list of Position objects."""
    return get_client().get_all_positions()


@_retry
def get_order(order_id: str) -> Optional[Order]:
    """
    Fetch a full order object by ID.
    Used to get fill price after a trade executes.
    Returns None if the order genuinely doesn't exist; retries on transient errors.
    """
    try:
        return get_client().get_order_by_id(order_id)
    except APIError as exc:
        if "order not found" in str(exc).lower() or "40410000" in str(exc):
            return None
        raise
    except Exception as exc:
        log.warning("Could not fetch order %s: %s", order_id, exc)
        return None


def is_market_open() -> bool:
    """Return whether US equities markets are open right now, via Alpaca's clock.

    Used as a broker-agnostic signal for Robinhood order classification, since
    robin_stocks returns the same "unconfirmed" acknowledgment state for orders
    regardless of whether they'll fill immediately or queue for the next session.
    Defaults to True on error — most orders are placed during market hours, so
    this minimises the more common misclassification.
    """
    try:
        return bool(get_client().get_clock().is_open)
    except Exception as exc:
        log.warning("Could not fetch market clock — assuming market is open: %s", exc)
        return True


def was_market_open_today() -> bool:
    """Return True if today is an actual trading session (not a holiday).

    is_market_open() checks the live clock — always False at 4 PM ET even on
    normal days. This checks Alpaca's calendar for today's date instead.
    Defaults to True on error so snapshots aren't silently dropped.
    """
    today = date.today()
    try:
        req = GetCalendarRequest(start=today, end=today)
        calendars = get_client().get_calendar(req)
        return len(calendars) > 0
    except Exception as exc:
        log.warning("Could not check market calendar for today — assuming open: %s", exc)
        return True


def get_next_trading_day() -> date:
    """Return the next trading day using Alpaca's market calendar, with a weekday fallback."""
    today = date.today()
    try:
        req = GetCalendarRequest(
            start=today + timedelta(days=1),
            end=today + timedelta(days=7),
        )
        calendars = get_client().get_calendar(req)
        if calendars:
            return calendars[0].date
    except Exception as exc:
        log.warning("Could not fetch trading calendar: %s", exc)
    next_day = today + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day


def get_alpaca_deposit_events() -> list[tuple[str, float]]:
    """Return [(iso_date, amount), ...] for cash deposits into the Alpaca account.

    Queries the account activities API for CSD (cash deposit) and JNLC (journal
    cash contribution) activity types — both represent external capital additions.
    Returns an empty list on any error so charts fall back to raw equity.
    """
    try:
        # Lazy import — GetAccountActivitiesRequest was added in alpaca-py 0.8+;
        # if it's missing we gracefully return [] rather than crashing on startup.
        from alpaca.trading.requests import GetAccountActivitiesRequest  # noqa: PLC0415
        req = GetAccountActivitiesRequest(activity_types=["CSD", "JNLC"])
        activities = get_client().get_account_activities(activity_filter=req)
        events = []
        for act in activities or []:
            try:
                net = float(act.net_amount)
                if net <= 0:
                    continue
                act_date = act.date if isinstance(act.date, str) else act.date.isoformat()
                events.append((act_date[:10], net))
            except Exception:
                continue
        return sorted(events)
    except Exception as exc:
        log.warning("Could not fetch Alpaca deposit activities: %s", exc)
        return []


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
