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
from datetime import datetime, timezone
from typing import Optional

import robin_stocks.robinhood as r

from app.config import settings
from app.trading import alpaca_client as ac

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

        # Whether the order will queue for the next session rather than fill now.
        # robin_stocks always returns state="unconfirmed" on submission regardless
        # of market hours, so that field can't tell us this — Alpaca's market
        # clock can, since both brokers trade the same US equities session.
        queued = not ac.is_market_open()

        if action in (TradingAction.BUY, TradingAction.ADD_LEVERAGE):
            qty, price_est = self._calculate_buy_qty(ticker)
            order = self._place_market_buy(ticker, qty)
            if queued:
                return {"status": "ok", "side": "buy", "qty": qty, "price_est": price_est, "position_qty": qty, "order_id": order.get("id"), "queued": True}
            fill_price = float(order.get("average_price") or 0) or price_est
            position_qty = self._get_position_qty(ticker) or qty
            return {"status": "ok", "side": "buy", "qty": qty, "fill_price": fill_price, "position_qty": position_qty, "order_id": order.get("id")}

        elif action == TradingAction.REVERSE_TO_LONG:
            self._close_position(ticker)  # close short if any (no-op on standard accounts)
            qty, price_est = self._calculate_buy_qty(ticker)
            order = self._place_market_buy(ticker, qty)
            if queued:
                return {"status": "ok", "side": "buy", "qty": qty, "price_est": price_est, "position_qty": qty, "order_id": order.get("id"), "queued": True}
            fill_price = float(order.get("average_price") or 0) or price_est
            position_qty = self._get_position_qty(ticker) or qty
            return {"status": "ok", "side": "buy", "qty": qty, "fill_price": fill_price, "position_qty": position_qty, "order_id": order.get("id")}

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
            if queued:
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

    def _calculate_buy_qty(
        self, ticker: str, fraction: Optional[float] = None
    ) -> tuple[float, float]:
        """Returns (qty, price_used) so callers can use price as fill_price estimate."""
        buying_power = self._get_buying_power()
        price        = self._get_latest_price(ticker)
        f            = fraction if fraction is not None else settings.rh_leverage_factor
        qty          = round((buying_power * f) / price, 6)
        if qty <= 0:
            raise ValueError(
                f"Robinhood buy qty is 0 — buying_power={buying_power}, "
                f"price={price}, fraction={f}"
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

    def _get_position_qty(self, ticker: str) -> Optional[float]:
        """Return total quantity held for ticker, or None if no position / error."""
        try:
            pos = self._get_position(ticker)
            if pos is None:
                return None
            return float(pos.get("quantity", 0)) or None
        except Exception:
            return None

    def get_all_positions_detail(self) -> list:
        """Return all open RH stock positions with current price and unrealized P&L."""
        raw = r.get_open_stock_positions(account_number=self._account_number()) or []
        result = []
        for pos in raw:
            try:
                instrument = r.get_instrument_by_url(pos.get("instrument", "")) or {}
                symbol = instrument.get("symbol", "").upper()
                if not symbol:
                    continue
                qty = float(pos.get("quantity", 0))
                if qty <= 0:
                    continue
                avg_entry = float(pos.get("average_buy_price") or 0)
                prices = r.get_latest_price(symbol)
                current = float(prices[0]) if prices and prices[0] else avg_entry
                unreal_pl = (current - avg_entry) * qty
                unreal_plpc = (current - avg_entry) / avg_entry * 100 if avg_entry else 0.0
                result.append({
                    "symbol": symbol,
                    "qty": qty,
                    "avg_entry_price": avg_entry,
                    "current_price": current,
                    "unrealized_pl": unreal_pl,
                    "unrealized_plpc": unreal_plpc,
                })
            except Exception as exc:
                log.warning("RH position detail failed: %s", exc)
        return result

    async def get_all_positions_async(self) -> list:
        """Async wrapper for get_all_positions_detail."""
        if not self.available:
            return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self.get_all_positions_detail)
        except Exception as exc:
            log.warning("RH get_all_positions_async failed: %s", exc)
            return []

    async def get_buying_power_async(self) -> Optional[float]:
        """Async wrapper for _get_buying_power."""
        if not self.available:
            return None
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._get_buying_power)
        except Exception as exc:
            log.warning("RH get_buying_power_async failed: %s", exc)
            return None

    def _get_equity_historicals_sync(
        self, span: str, interval: str, since: Optional[datetime] = None
    ) -> list[dict]:
        """Fetch equity_historicals from robin_stocks for `span`/`interval`.

        If `since` is given, only entries at or after that timestamp are kept —
        robin_stocks has no native "ytd" span, so YTD is approximated by
        filtering a "year" span.
        """
        data = r.get_historical_portfolio(interval=interval, span=span, bounds="regular") or {}
        historicals = data.get("equity_historicals") or []
        if since is not None:
            historicals = [h for h in historicals if _parse_rh_timestamp(h.get("begins_at")) >= since]
        return historicals

    def _get_portfolio_pct_change_sync(
        self, span: str, interval: str, since: Optional[datetime] = None
    ) -> Optional[float]:
        """Compute the RH portfolio's % return over `span` from historical equity.

        Uses adjusted equity (excludes the effect of deposits/withdrawals) so the
        result is comparable to a price return like SPY's.
        """
        historicals = self._get_equity_historicals_sync(span, interval, since)
        if not historicals:
            return None
        open_eq = float(historicals[0].get("adjusted_open_equity") or historicals[0].get("open_equity") or 0)
        close_eq = float(historicals[-1].get("adjusted_close_equity") or historicals[-1].get("close_equity") or 0)
        if not open_eq:
            return None
        return (close_eq - open_eq) / open_eq * 100

    async def get_portfolio_pct_change_async(
        self, span: str, interval: str, since: Optional[datetime] = None
    ) -> Optional[float]:
        """Async wrapper for _get_portfolio_pct_change_sync. Never raises."""
        if not self.available:
            return None
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._get_portfolio_pct_change_sync, span, interval, since)
        except Exception as exc:
            log.warning("RH get_portfolio_pct_change_async failed: %s", exc)
            return None

    async def get_equity_history_async(
        self, span: str, interval: str, since: Optional[datetime] = None
    ) -> Optional[tuple[list[float], list[int]]]:
        """Return (equity_values, unix_timestamps) for charting, or None on failure/empty.

        Equity values prefer adjusted_close_equity (excludes deposit/withdrawal
        effects) falling back to close_equity. Timestamps come from each entry's
        begins_at.
        """
        if not self.available:
            return None
        loop = asyncio.get_running_loop()
        try:
            historicals = await loop.run_in_executor(
                None, self._get_equity_historicals_sync, span, interval, since
            )
        except Exception as exc:
            log.warning("RH get_equity_history_async failed: %s", exc)
            return None
        if not historicals:
            return None
        equity: list[float] = []
        timestamps: list[int] = []
        for h in historicals:
            eq = h.get("adjusted_close_equity") or h.get("close_equity")
            if eq is None:
                continue
            equity.append(float(eq))
            timestamps.append(int(_parse_rh_timestamp(h.get("begins_at")).timestamp()))
        if not equity:
            return None
        return equity, timestamps

    def _close_ticker_sync(self, ticker: str) -> dict:
        pos = self._get_position(ticker)
        if pos is None:
            return {"status": "ok", "note": "no position to close", "qty": 0.0}
        qty = float(pos.get("quantity", 0))
        if qty <= 0:
            return {"status": "ok", "note": "no position to close", "qty": 0.0}
        price_est = self._get_latest_price(ticker)
        order = self._place_market_sell(ticker, qty)
        queued = not ac.is_market_open()
        if queued:
            return {"status": "ok", "qty": qty, "price_est": price_est, "queued": True, "order_id": order.get("id")}
        fill_price = float(order.get("average_price") or 0) or price_est
        return {"status": "ok", "qty": qty, "fill_price": fill_price, "order_id": order.get("id")}

    async def close_ticker_async(self, ticker: str) -> dict:
        """Close the full position for ticker. Returns a status dict."""
        if not self.available:
            return {"status": "skipped", "reason": "session unavailable"}
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._close_ticker_sync, ticker)
        except Exception as exc:
            if _is_auth_error(exc):
                self.available = False
                return {"status": "failed", "reason": "session expired"}
            return {"status": "failed", "reason": str(exc)}

    def _close_position(self, ticker: str) -> Optional[dict]:
        pos = self._get_position(ticker)
        if pos is None:
            return None
        qty = float(pos.get("quantity", 0))
        if qty <= 0:
            return None
        return self._place_market_sell(ticker, qty)

    def _buy_dollars_sync(self, ticker: str, dollars: float) -> dict:
        """Buy a specific dollar amount of ticker. Used by the Claude portfolio manager."""
        price = self._get_latest_price(ticker)
        qty = round(dollars / price, 6)
        if qty <= 0:
            raise ValueError(f"Buy qty is 0 for {ticker} at ${price:.2f} with ${dollars:.2f}")
        order = self._place_market_buy(ticker, qty)
        queued = not ac.is_market_open()
        if queued:
            return {"status": "ok", "qty": qty, "price_est": price, "queued": True}
        fill_price = float(order.get("average_price") or 0) or price
        return {"status": "ok", "qty": qty, "fill_price": fill_price, "order_id": order.get("id")}

    def _sell_shares_sync(self, ticker: str, qty: float) -> dict:
        """Sell a specific quantity of ticker. Used for TRIM (partial close)."""
        price_est = self._get_latest_price(ticker)
        order = self._place_market_sell(ticker, qty)
        queued = not ac.is_market_open()
        if queued:
            return {"status": "ok", "qty": qty, "price_est": price_est, "queued": True, "order_id": order.get("id")}
        fill_price = float(order.get("average_price") or 0) or price_est
        return {"status": "ok", "qty": qty, "fill_price": fill_price, "order_id": order.get("id")}

    async def sell_shares_async(self, ticker: str, qty: float) -> dict:
        """Sell a specific share quantity (for TRIM). Never raises."""
        if not self.available:
            return {"status": "skipped", "reason": "session unavailable"}
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._sell_shares_sync, ticker, qty)
        except Exception as exc:
            if _is_auth_error(exc):
                self.available = False
                return {"status": "failed", "reason": "session expired"}
            return {"status": "failed", "reason": str(exc)}

    async def buy_dollars_async(self, ticker: str, dollars: float) -> dict:
        """Async wrapper for _buy_dollars_sync. Never raises."""
        if not self.available:
            return {"status": "skipped", "reason": "session unavailable"}
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._buy_dollars_sync, ticker, dollars)
        except Exception as exc:
            if _is_auth_error(exc):
                self.available = False
                return {"status": "failed", "reason": "session expired"}
            return {"status": "failed", "reason": str(exc)}

    async def execute_for_claude(self, action: str, ticker: str) -> dict:
        """Execute a Claude portfolio trade using CLAUDE_LEVERAGE_FACTOR sizing. Never raises."""
        if not settings.rh_enabled:
            return {"status": "skipped", "reason": "RH_ENABLED=false"}
        if not self.available:
            return {"status": "skipped", "reason": "session unavailable"}
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, self._execute_claude_sync, action.upper(), ticker.upper()
            )
        except Exception as exc:
            if _is_auth_error(exc):
                self.available = False
                log.error("Robinhood auth error (Claude trade) — marking unavailable: %s", exc)
                return {"status": "failed", "reason": "session expired"}
            log.error("Claude portfolio trade error %s %s: %s", action, ticker, exc)
            return {"status": "failed", "reason": str(exc)}

    def _execute_claude_sync(self, action: str, ticker: str) -> dict:
        queued = not ac.is_market_open()
        fraction = settings.claude_leverage_factor

        if action == "BUY":
            qty, price_est = self._calculate_buy_qty(ticker, fraction=fraction)
            order = self._place_market_buy(ticker, qty)
            if queued:
                return {"status": "ok", "side": "buy", "qty": qty, "price_est": price_est,
                        "position_qty": qty, "order_id": order.get("id"), "queued": True}
            fill_price = float(order.get("average_price") or 0) or price_est
            position_qty = self._get_position_qty(ticker) or qty
            return {"status": "ok", "side": "buy", "qty": qty, "fill_price": fill_price,
                    "position_qty": position_qty, "order_id": order.get("id")}

        elif action == "SELL":
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
            if queued:
                return {"status": "ok", "side": "sell", "qty": qty, "price_est": price_est,
                        "avg_buy_price": avg_buy_price, "position_qty": 0.0,
                        "order_id": order.get("id"), "queued": True}
            fill_price = float(order.get("average_price") or 0) or price_est
            return {"status": "ok", "side": "sell", "qty": qty, "fill_price": fill_price,
                    "avg_buy_price": avg_buy_price, "position_qty": 0.0, "order_id": order.get("id")}

        return {"status": "skipped", "reason": f"unknown action: {action}"}


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("token", "unauthorized", "401", "login", "unauthenticated"))


def _parse_rh_timestamp(ts: Optional[str]) -> datetime:
    """Parse a robin_stocks historicals "begins_at" timestamp (e.g. "2026-01-01T00:00:00Z")."""
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# Module-level singleton — import this in order_logic.py and main.py
rh_client = RobinhoodClient()
