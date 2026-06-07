"""
robinhood_client.py — Robinhood authentication and order execution.

Uses robin_stocks (unofficial Robinhood API). Since robin_stocks is
synchronous, all trade calls run via asyncio.run_in_executor so they
don't block FastAPI's event loop.

Session token is stored at /data/robinhood.pickle on Render's persistent
disk. On startup, login_from_pickle() restores the session automatically.
When the token expires, login_with_sms() re-authenticates using an SMS
code and saves a fresh token.
"""

import asyncio
import logging
import os
import shutil
from typing import Optional

import robin_stocks.robinhood as r

from app.config import settings

log = logging.getLogger(__name__)

_PICKLE_BACKUP = "/data/robinhood.pickle"
_TOKENS_DIR    = os.path.expanduser("~/.tokens")
_PICKLE_PATH   = os.path.join(_TOKENS_DIR, "robinhood.pickle")


class RobinhoodClient:
    def __init__(self):
        self.available: bool = False

    # ── Auth ────────────────────────────────────────────────────────────────────

    def login_from_pickle(self) -> bool:
        """Restore session from /data/robinhood.pickle. Returns True if valid."""
        if not settings.rh_enabled:
            return False
        if not os.path.exists(_PICKLE_BACKUP):
            log.warning("Robinhood pickle not found at %s", _PICKLE_BACKUP)
            return False
        os.makedirs(_TOKENS_DIR, exist_ok=True)
        shutil.copy2(_PICKLE_BACKUP, _PICKLE_PATH)
        try:
            r.login(
                username=settings.rh_username,
                password=settings.rh_password,
                store_session=True,
            )
            r.load_account_profile()  # verify token is actually valid
            # Save refreshed token back to persistent disk
            if os.path.exists(_PICKLE_PATH):
                shutil.copy2(_PICKLE_PATH, _PICKLE_BACKUP)
            self.available = True
            log.info("Robinhood session restored from pickle")
            return True
        except Exception as exc:
            log.warning("Robinhood pickle login failed: %s", exc)
            self.available = False
            return False

    async def keep_alive(self) -> None:
        """Refresh the session token. Runs on a schedule to prevent expiry."""
        from app.notifications import notify_rh_session
        if not settings.rh_enabled:
            return
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, self.login_from_pickle)
        if ok:
            log.info("Robinhood session refreshed via keep-alive")
            await notify_rh_session(
                "🔄 **ROBINHOOD SESSION REFRESHED**\n"
                "Auto keep-alive successful — session remains active."
            )
        else:
            log.warning("Robinhood keep-alive failed — session expired")
            await notify_rh_session(
                "⚠️ **ROBINHOOD SESSION EXPIRED**\n"
                "Keep-alive failed — session is dead.\n"
                "Run the two local commands to re-authenticate and re-upload the pickle."
            )

    def login_with_sms(self, sms_code: str) -> None:
        """Authenticate with SMS 2FA code. Saves new session to disk. Raises on failure."""
        r.login(
            username=settings.rh_username,
            password=settings.rh_password,
            mfa_code=sms_code,
            store_session=True,
        )
        os.makedirs(_TOKENS_DIR, exist_ok=True)
        if os.path.exists(_PICKLE_PATH):
            os.makedirs(os.path.dirname(_PICKLE_BACKUP) or "/data", exist_ok=True)
            shutil.copy2(_PICKLE_PATH, _PICKLE_BACKUP)
        self.available = True
        log.info("Robinhood authenticated via SMS code")

    # ── Trade execution ─────────────────────────────────────────────────────────

    async def execute(self, action, ticker: str) -> dict:
        """
        Execute a trade action on Robinhood. Never raises.
        Returns a status dict: {"status": "ok"|"failed"|"skipped", ...}
        """
        from app.models import TradingAction

        if not settings.rh_enabled:
            return {"status": "skipped", "reason": "RH_ENABLED=false"}
        if not self.available:
            return {"status": "skipped", "reason": "session unavailable"}

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._execute_sync, action, ticker)
        except Exception as exc:
            if _is_auth_error(exc):
                self.available = False
                log.error("Robinhood auth error — marking unavailable: %s", exc)
                return {"status": "failed", "reason": "session expired"}
            log.error("Robinhood trade error %s %s: %s", action, ticker, exc)
            return {"status": "failed", "reason": str(exc)}

    def _execute_sync(self, action, ticker: str) -> dict:
        from app.models import TradingAction

        if action in (TradingAction.BUY, TradingAction.ADD_LEVERAGE):
            qty, price_est = self._calculate_buy_qty(ticker)
            order = self._place_market_buy(ticker, qty)
            if _is_queued(order):
                return {"status": "ok", "side": "buy", "qty": qty, "price_est": price_est, "position_qty": qty, "order_id": order.get("id"), "queued": True}
            fill_price = float(order.get("average_price") or 0) or price_est
            return {"status": "ok", "side": "buy", "qty": qty, "fill_price": fill_price, "position_qty": qty, "order_id": order.get("id")}

        elif action == TradingAction.REVERSE_TO_LONG:
            self._close_position(ticker)  # close short if any (no-op on standard accounts)
            qty, price_est = self._calculate_buy_qty(ticker)
            order = self._place_market_buy(ticker, qty)
            if _is_queued(order):
                return {"status": "ok", "side": "buy", "qty": qty, "price_est": price_est, "position_qty": qty, "order_id": order.get("id"), "queued": True}
            fill_price = float(order.get("average_price") or 0) or price_est
            return {"status": "ok", "side": "buy", "qty": qty, "fill_price": fill_price, "position_qty": qty, "order_id": order.get("id")}

        elif action in (TradingAction.SELL, TradingAction.CLOSE_LONG,
                        TradingAction.REMOVE_LEVERAGE, TradingAction.STOP_LOSS):
            pos = self._get_position(ticker)
            if pos is None:
                return {"status": "ok", "note": "no position to close"}
            qty = float(pos.get("quantity", 0))
            if qty <= 0:
                return {"status": "ok", "note": "no position to close"}
            avg_buy_price_raw = pos.get("average_buy_price")
            avg_buy_price = float(avg_buy_price_raw) if avg_buy_price_raw else None
            price_est = self._get_latest_price(ticker)
            order = self._place_market_sell(ticker, qty)
            if _is_queued(order):
                return {"status": "ok", "side": "sell", "qty": qty, "price_est": price_est, "avg_buy_price": avg_buy_price, "position_qty": 0.0, "order_id": order.get("id"), "queued": True}
            fill_price = float(order.get("average_price") or 0) or price_est
            return {
                "status": "ok", "side": "sell",
                "qty": qty, "fill_price": fill_price,
                "avg_buy_price": avg_buy_price, "position_qty": 0.0,
                "order_id": order.get("id"),
            }

        elif action in (TradingAction.CLOSE_SHORT, TradingAction.REVERSE_TO_SHORT):
            # Shorting not supported on standard Robinhood accounts.
            # Close any long position if present; skip the short open.
            order = self._close_position(ticker)
            return {
                "status": "ok",
                "note": "short not supported — closed long if present",
                "closed_long": order is not None,
            }

        elif action == TradingAction.BASE_ENTRY:
            return {"status": "skipped", "reason": "base_entry intentionally skipped"}

        return {"status": "skipped", "reason": f"unknown action: {action}"}

    # ── Private helpers ─────────────────────────────────────────────────────────

    def _calculate_buy_qty(self, ticker: str) -> tuple[float, float]:
        """Returns (qty, price_used) so callers can use price as fill_price estimate."""
        buying_power = self._get_buying_power()
        price        = self._get_latest_price(ticker)
        qty          = round((buying_power * settings.rh_leverage_factor) / price, 6)
        if qty <= 0:
            raise ValueError(
                f"Robinhood buy qty is 0 — buying_power={buying_power}, "
                f"price={price}, rh_leverage_factor={settings.rh_leverage_factor}"
            )
        return qty, price

    def _account_number(self) -> Optional[str]:
        return settings.rh_account_number or None

    def _get_buying_power(self) -> float:
        profile = r.load_account_profile(account_number=self._account_number()) or {}
        bp = profile.get("cash") or profile.get("portfolio_cash") or "0"
        return float(bp)

    def _get_latest_price(self, ticker: str) -> float:
        prices = r.get_latest_price(ticker)
        if not prices or prices[0] is None:
            raise ValueError(f"Could not get price for {ticker}")
        return float(prices[0])

    def _place_market_buy(self, ticker: str, qty: float) -> dict:
        result = r.order_buy_fractional_by_quantity(
            ticker, qty, account_number=self._account_number()
        ) or {}
        if not result.get("id") and not result.get("cancel"):
            raise ValueError(f"Robinhood order rejected: {result}")
        log.info("Robinhood BUY placed", extra={"ticker": ticker, "qty": qty})
        return result

    def _place_market_sell(self, ticker: str, qty: float) -> dict:
        result = r.order_sell_fractional_by_quantity(
            ticker, qty, account_number=self._account_number()
        ) or {}
        if not result.get("id") and not result.get("cancel"):
            raise ValueError(f"Robinhood sell order rejected: {result}")
        log.info("Robinhood SELL placed", extra={"ticker": ticker, "qty": qty})
        return result

    def _get_position(self, ticker: str) -> Optional[dict]:
        positions = r.get_open_stock_positions(account_number=self._account_number())
        for pos in (positions or []):
            try:
                instrument = r.get_instrument_by_url(pos.get("instrument", "")) or {}
            except Exception:
                continue
            if instrument.get("symbol", "").upper() == ticker.upper():
                return pos
        return None

    def _close_position(self, ticker: str) -> Optional[dict]:
        pos = self._get_position(ticker)
        if pos is None:
            return None
        qty = float(pos.get("quantity", 0))
        if qty <= 0:
            return None
        return self._place_market_sell(ticker, qty)


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("token", "unauthorized", "401", "login", "unauthenticated"))


def _is_queued(order: dict) -> bool:
    return order.get("state") in ("queued", "unconfirmed")


# Module-level singleton — import this in order_logic.py and main.py
rh_client = RobinhoodClient()
