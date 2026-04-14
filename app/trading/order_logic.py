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

log = logging.getLogger(__name__)

# Kimi leverage_factor — must match what is set in TradingView script (default 0.5)
LEVERAGE_FACTOR = 0.5


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
        order = _kimi_add_leverage(ticker, payload.price)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "DD order skipped — insufficient buying power."

    elif action == TradingAction.REMOVE_LEVERAGE:
        # Close only the DD (Leverage) position, leave base untouched
        order = _kimi_remove_leverage(ticker)
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

def _kimi_add_leverage(ticker: str, price: Optional[float]) -> Optional[Order]:
    """
    Calculate DD qty from real Alpaca buying power and place the buy order.

    Mirrors Kimi script logic:
        leverage_qty = (strategy.equity * leverage_factor) / close

    Here we use Alpaca's actual buying_power instead of strategy.equity
    so the sizing is always based on real available funds.
    """
    account = ac.get_account()
    buying_power = float(account.buying_power)

    # Use price from alert payload if available, otherwise fetch from Alpaca
    if price and price > 0:
        current_price = price
    else:
        current_price = ac.get_latest_price(ticker)

    if not current_price or current_price <= 0:
        raise ValueError(f"Could not determine current price for {ticker}")

    # Calculate DD qty — same formula as Kimi script
    raw_qty = (buying_power * LEVERAGE_FACTOR) / current_price
    dd_qty  = math.floor(raw_qty)  # whole shares only

    if dd_qty <= 0:
        log.warning(
            "DD qty is 0 — not enough buying power",
            extra={
                "ticker":        ticker,
                "buying_power":  buying_power,
                "price":         current_price,
                "leverage_factor": LEVERAGE_FACTOR,
            },
        )
        return None

    log.info(
        "Placing Kimi DD buy",
        extra={
            "ticker":        ticker,
            "buying_power":  buying_power,
            "price":         current_price,
            "dd_qty":        dd_qty,
        },
    )

    return ac.place_market_order(ticker, OrderSide.BUY, dd_qty)


def _kimi_remove_leverage(ticker: str) -> Optional[Order]:
    """
    Close the DD (leverage) position only.
    Looks for an open position on the ticker and closes it partially
    by the DD qty tracked via Alpaca positions.

    Since Alpaca merges all buys into one position, we track the DD
    qty by looking at the difference between total position and base qty.
    If tracking is unavailable, we close the full position as a fallback.
    """
    position = ac.get_position(ticker)

    if position is None:
        log.info("No position found to remove leverage from", extra={"ticker": ticker})
        return None

    total_qty = float(position.qty)

    # Try to close only the DD portion
    # Since Alpaca merges positions, we close half the total as an approximation
    # This works because base ≈ equity/price and DD ≈ equity*0.5/price
    # so DD is roughly 1/3 of total position
    # For a cleaner solution, store DD qty in a database/cache when ADD_LEVERAGE fires
    dd_qty = math.floor(total_qty * (LEVERAGE_FACTOR / (1 + LEVERAGE_FACTOR)))

    if dd_qty <= 0:
        log.warning("Calculated DD qty to close is 0", extra={"ticker": ticker, "total_qty": total_qty})
        return None

    log.info(
        "Closing Kimi DD position",
        extra={"ticker": ticker, "total_qty": total_qty, "closing_dd_qty": dd_qty},
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
