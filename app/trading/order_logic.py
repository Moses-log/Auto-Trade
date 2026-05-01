"""
order_logic.py — Translates TradingView alert actions into Alpaca orders.

Action mapping
──────────────
buy              → BUY qty shares
sell             → SELL qty shares
close_long       → close any open long position (all shares)
close_short      → close any open short position (buy to cover)
reverse_to_long  → close short (if any) then BUY qty shares
reverse_to_short → close long  (if any) then SELL qty shares

Kimi strategy actions
──────────────────────
base_entry       → ignored (you place the base order manually on Alpaca)
add_leverage     → query Alpaca buying power, calculate DD qty, place BUY
remove_leverage  → close only the "Leverage" position on Alpaca
stop_loss        → close ALL open positions on Alpaca
"""

import logging
import math
from typing import Optional

from alpaca.trading.enums import OrderSide
from alpaca.trading.models import Order

from app.models import AlertPayload, TradingAction
from app.trading import alpaca_client as ac
from app.config import settings

log = logging.getLogger(__name__)

# Default leverage factor — overridden per-alert by payload.leverage_factor
DEFAULT_LEVERAGE_FACTOR = 0.5


# ── Entry point ───────────────────────────────────────────────────────────────

async def execute_action(payload: AlertPayload) -> dict:
    """
    Dispatch the alert action and return a summary dict for the HTTP response.
    """
    action = payload.action
    ticker = payload.ticker
    qty    = payload.contracts

    log.info(
        "Executing action",
        extra={"action": action, "ticker": ticker, "qty": qty},
    )

    result: dict = {"action": action, "ticker": ticker, "orders": []}

    # ── Legacy actions ────────────────────────────────────────────────────────

    if action == TradingAction.BUY:
        order = _require_qty_then_order(ticker, OrderSide.BUY, qty)
        result["orders"].append(_order_summary(order))

    elif action == TradingAction.SELL:
        order = _require_qty_then_order(ticker, OrderSide.SELL, qty)
        result["orders"].append(_order_summary(order))

    elif action == TradingAction.CLOSE_LONG:
        order = _close_if_long(ticker)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "No long position to close."

    elif action == TradingAction.CLOSE_SHORT:
        order = _close_if_short(ticker)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "No short position to close."

    elif action == TradingAction.REVERSE_TO_LONG:
        close_order = _close_if_short(ticker)
        if close_order:
            result["orders"].append(_order_summary(close_order))
        long_order = _require_qty_then_order(ticker, OrderSide.BUY, qty)
        result["orders"].append(_order_summary(long_order))

    elif action == TradingAction.REVERSE_TO_SHORT:
        close_order = _close_if_long(ticker)
        if close_order:
            result["orders"].append(_order_summary(close_order))
        short_order = _require_qty_then_order(ticker, OrderSide.SELL, qty)
        result["orders"].append(_order_summary(short_order))

    # ── Kimi strategy actions ─────────────────────────────────────────────────

    elif action == TradingAction.BASE_ENTRY:
        # Base order is placed manually — bot intentionally does nothing here
        log.info("Base entry signal received — no action taken (place manually on Alpaca)")
        result["note"] = "Base entry ignored — place base order manually on Alpaca."

    elif action == TradingAction.ADD_LEVERAGE:
        # Query real Alpaca buying power and calculate DD qty
        lf = payload.leverage_factor if payload.leverage_factor is not None else DEFAULT_LEVERAGE_FACTOR
        order = _kimi_add_leverage(ticker, payload.price, lf)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "DD order skipped — insufficient buying power."

    elif action == TradingAction.REMOVE_LEVERAGE:
        # Close only the DD (Leverage) position, leave base untouched
        lf = payload.leverage_factor if payload.leverage_factor is not None else DEFAULT_LEVERAGE_FACTOR
        order = _kimi_remove_leverage(ticker, lf)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "No leverage position to close."

    elif action == TradingAction.STOP_LOSS:
        # Close everything
        orders = _kimi_stop_loss(ticker)
        result["orders"].extend([_order_summary(o) for o in orders if o])
        if not result["orders"]:
            result["note"] = "No open positions to close."

    else:
        raise ValueError(f"Unknown action: {action}")

    return result


# ── Kimi-specific helpers ─────────────────────────────────────────────────────

def _kimi_add_leverage(ticker: str, price: Optional[float], leverage_factor: float) -> Optional[Order]:
    """
    Calculate DD qty from real Alpaca buying power and place the buy order.
    leverage_factor comes directly from TradingView alert payload.
    """
    account = ac.get_account()
    buying_power = float(account.buying_power)

    if price and price > 0:
        current_price = price
    else:
        current_price = ac.get_latest_price(ticker)

    if not current_price or current_price <= 0:
        raise ValueError(f"Could not determine current price for {ticker}")

    raw_qty = (buying_power * leverage_factor) / current_price
    dd_qty  = round(raw_qty, 2) if settings.allow_fractional_shares else math.floor(raw_qty)

    if dd_qty <= 0:
        log.warning(
            "DD qty is 0 — not enough buying power",
            extra={
                "ticker":          ticker,
                "buying_power":    buying_power,
                "price":           current_price,
                "leverage_factor": leverage_factor,
            },
        )
        return None

    log.info(
        "Placing Kimi DD buy",
        extra={
            "ticker":          ticker,
            "buying_power":    buying_power,
            "price":           current_price,
            "leverage_factor": leverage_factor,
            "dd_qty":          dd_qty,
        },
    )

    return ac.place_market_order(ticker, OrderSide.BUY, dd_qty)


def _kimi_remove_leverage(ticker: str, leverage_factor: float) -> Optional[Order]:
    """
    Close the DD (leverage) position only.
    Uses leverage_factor from payload to calculate DD portion of total position.
    """
    position = ac.get_position(ticker)

    if position is None:
        log.info("No position found to remove leverage from", extra={"ticker": ticker})
        return None

    total_qty = float(position.qty)
    dd_portion = total_qty * (leverage_factor / (1 + leverage_factor))
    dd_qty = round(dd_portion, 2) if settings.allow_fractional_shares else math.floor(dd_portion)

    if dd_qty <= 0:
        log.warning("Calculated DD qty to close is 0", extra={"ticker": ticker, "total_qty": total_qty})
        return None

    log.info(
        "Closing Kimi DD position",
        extra={"ticker": ticker, "total_qty": total_qty, "closing_dd_qty": dd_qty, "leverage_factor": leverage_factor},
    )

    return ac.place_market_order(ticker, OrderSide.SELL, dd_qty)


def _kimi_stop_loss(ticker: str) -> list:
    """Close all open positions for the ticker."""
    orders = []
    position = ac.get_position(ticker)
    if position:
        log.info("Stop loss triggered — closing all positions", extra={"ticker": ticker})
        order = ac.close_position(ticker)
        if order:
            orders.append(order)
    return orders


# ── Private helpers ───────────────────────────────────────────────────────────

def _require_qty_then_order(
    ticker: str,
    side: OrderSide,
    qty: Optional[float],
) -> Order:
    if qty is None or qty <= 0:
        raise ValueError(
            f"Action '{side.value}' requires a positive 'contracts' value, "
            f"got: {qty!r}. Check your TradingView alert message template."
        )
    return ac.place_market_order(ticker, side, qty)


def _close_if_long(ticker: str) -> Optional[Order]:
    position = ac.get_position(ticker)
    if position is None:
        return None
    if str(position.side).lower() != "long":
        log.info("Skipping close_long — position is not long", extra={"ticker": ticker})
        return None
    return ac.close_position(ticker)


def _close_if_short(ticker: str) -> Optional[Order]:
    position = ac.get_position(ticker)
    if position is None:
        return None
    if str(position.side).lower() != "short":
        log.info("Skipping close_short — position is not short", extra={"ticker": ticker})
        return None
    return ac.close_position(ticker)


def _order_summary(order: Order) -> dict:
    return {
        "alpaca_order_id": str(order.id),
        "symbol":          order.symbol,
        "side":            str(order.side),
        "qty":             str(order.qty),
        "type":            str(order.order_type),
        "status":          str(order.status),
    }
