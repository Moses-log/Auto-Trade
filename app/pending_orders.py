from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_FILE = Path(os.getenv("PENDING_ORDERS_PATH", "pending_orders.json"))
_lock = threading.Lock()


def _load() -> list:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text()).get("pending", [])
        except Exception:
            return []
    return []


def _save(orders: list) -> None:
    _FILE.write_text(json.dumps({"pending": orders}))


def save_pending_order(
    order_id: str,
    ticker: str,
    action: str,
    alert_price: Optional[float],
    avg_entry_price: Optional[float],
    run_at: str,
    broker: str = "alpaca",
    **extra,
) -> None:
    with _lock:
        orders = _load()
        entry = {
            "order_id": order_id,
            "ticker": ticker,
            "action": action,
            "alert_price": alert_price,
            "avg_entry_price": avg_entry_price,
            "run_at": run_at,
            "broker": broker,
        }
        entry.update(extra)
        orders.append(entry)
        _save(orders)
    log.info("Saved pending %s order %s to disk", broker, order_id)


def remove_pending_order(order_id: str) -> None:
    with _lock:
        orders = _load()
        orders = [o for o in orders if o["order_id"] != order_id]
        _save(orders)
    log.info("Removed pending order %s from disk", order_id)


def load_pending_orders() -> list:
    return _load()
