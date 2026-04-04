"""
order_logic.py — Translates TradingView alert actions into Alpaca orders.

This is the core translation layer. Each action maps to one or two Alpaca
calls. Market orders are used throughout; add limit-order support here
when needed by swapping MarketOrderRequest for LimitOrderRequest in
alpaca_client.py and threading a `price` parameter through.

Action mapping
──────────────
buy              → BUY  qty shares
sell             → SELL qty shares
close_long       → close any open long position (all shares)
close_short      → close any open short position (buy to cover)
reverse_to_long  → close short (if any) then BUY qty shares
reverse_to_short → close long  (if any) then SELL qty shares

"close_long" and "close_short" intentionally close the *entire* position
rather than a fixed qty, because position size may have drifted from the
original order size (due to partial fills, corporate actions, etc.).
"""

import logging
from typing import Optional

from alpaca.trading.enums import OrderSide
from alpaca.trading.models import Order

from app.models import AlertPayload, TradingAction
from app.trading import alpaca_client as ac

log = logging.getLogger(__name__)


# ── Entry point ───────────────────────────────────────────────────────────────

async def execute_action(payload: AlertPayload) -> dict:
    """
    Dispatch the alert action and return a summary dict for the HTTP response.

    All Alpaca calls are synchronous (alpaca-py does not have async support)
    but they are fast enough for webhook latency. Wrap in run_in_executor
    if you hit event-loop blocking issues at scale.
    """
    action  = payload.action
    ticker  = payload.ticker
    qty     = payload.contracts  # may be None for close actions

    log.info(
        "Executing action",
        extra={"action": action, "ticker": ticker, "qty": qty},
    )

    result: dict = {"action": action, "ticker": ticker, "orders": []}

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
        # Step 1 — flatten short
        close_order = _close_if_short(ticker)
        if close_order:
            result["orders"].append(_order_summary(close_order))
        # Step 2 — go long
        long_order = _require_qty_then_order(ticker, OrderSide.BUY, qty)
        result["orders"].append(_order_summary(long_order))

    elif action == TradingAction.REVERSE_TO_SHORT:
        # Step 1 — flatten long
        close_order = _close_if_long(ticker)
        if close_order:
            result["orders"].append(_order_summary(close_order))
        # Step 2 — go short
        short_order = _require_qty_then_order(ticker, OrderSide.SELL, qty)
        result["orders"].append(_order_summary(short_order))

    else:
        # Should never reach here because Pydantic validates the action enum
        raise ValueError(f"Unknown action: {action}")

    return result


# ── Private helpers ───────────────────────────────────────────────────────────

def _require_qty_then_order(
    ticker: str,
    side: OrderSide,
    qty: Optional[float],
) -> Order:
    """Validate that qty is present and non-zero, then place the order."""
    if qty is None or qty <= 0:
        raise ValueError(
            f"Action '{side.value}' requires a positive 'contracts' value, "
            f"got: {qty!r}. Check your TradingView alert message template."
        )
    return ac.place_market_order(ticker, side, qty)


def _close_if_long(ticker: str) -> Optional[Order]:
    """Close the position only if it is currently long."""
    position = ac.get_position(ticker)
    if position is None:
        return None
    # alpaca-py PositionSide enum: 'long' / 'short'
    if str(position.side).lower() != "long":
        log.info(
            "Skipping close_long — position is not long",
            extra={"ticker": ticker, "side": str(position.side)},
        )
        return None
    return ac.close_position(ticker)


def _close_if_short(ticker: str) -> Optional[Order]:
    """Close the position only if it is currently short."""
    position = ac.get_position(ticker)
    if position is None:
        return None
    if str(position.side).lower() != "short":
        log.info(
            "Skipping close_short — position is not short",
            extra={"ticker": ticker, "side": str(position.side)},
        )
        return None
    return ac.close_position(ticker)


def _order_summary(order: Order) -> dict:
    """Serialise an Alpaca Order to a JSON-safe dict for the response body."""
    return {
        "alpaca_order_id": str(order.id),
        "symbol":          order.symbol,
        "side":            str(order.side),
        "qty":             str(order.qty),
        "type":            str(order.order_type),
        "status":          str(order.status),
    }
