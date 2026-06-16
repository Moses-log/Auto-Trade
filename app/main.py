"""
main.py — FastAPI application entry point.

Endpoints
─────────
POST /webhook   Receives TradingView alerts and routes them to Alpaca.
GET  /health    Liveness probe — returns 200 + uptime info.

Security model
──────────────
Every request to /webhook must carry the correct "secret" field in the
JSON body (matched via constant-time comparison in security.py). There is
no separate API-key header — the secret is embedded in the alert payload
as TradingView requires.
"""

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.idempotency import is_duplicate, mark_processed
from app.investors import Deposit, Investor, load_investors, save_investors, investors_lock
from app.logging_config import setup_logging
from app.models import AlertPayload, DepositRequest, TradingAction
from app.notifications import notify, close_http_client, notify_rh_session, notify_signal_feed
from app.rh_trade_notifier import notify_rh_trade
from app.pnl import (
    ET,
    send_daily_report,
    send_weekly_report,
    send_monthly_report,
    send_yearly_report,
    send_ytd_report,
    send_alltime_report,
    send_inception_report,
    send_custom_report,
    send_investor_report,
    _yf_executor,
)
from app.trade_notifier import notify_trade
from app.scheduler import scheduler, setup_jobs, reschedule_pending_orders
from app.security import verify_webhook_secret
from app.discord_commands import dispatch_command
from app.interactions import extract_user_id, parse_options, verify_discord_signature
from app.leverage_state import load_leverage_entry
from app.trading.alpaca_client import get_account, get_latest_price, get_position
from app.trading.order_logic import execute_action
from app.trading.robinhood_client import rh_client
from alpaca.common.exceptions import APIError

# ── Logging must be set up before the first log call ─────────────────────────
setup_logging()
log = logging.getLogger(__name__)

_start_time = time.time()

_SELL_ACTIONS = {
    TradingAction.SELL,
    TradingAction.CLOSE_LONG,
    TradingAction.CLOSE_SHORT,
    TradingAction.REVERSE_TO_LONG,
    TradingAction.REVERSE_TO_SHORT,
    TradingAction.REMOVE_LEVERAGE,
    TradingAction.STOP_LOSS,
}


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "TradingView → Alpaca webhook server starting",
        extra={"paper_trading": "paper" in settings.alpaca_base_url},
    )
    if settings.rh_enabled:
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, rh_client.login_from_pickle)
        if not ok:
            await notify_rh_session(
                "🚨 **ROBINHOOD SESSION OFFLINE**\n"
                "Session unavailable on startup — trading is paused.\n"
                "POST `/robinhood-auth` with your SMS code to activate."
            )
    setup_jobs()
    reschedule_pending_orders()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    _yf_executor.shutdown(wait=False)
    await close_http_client()
    log.info("Server shutting down.")


app = FastAPI(
    title="TradingView → Alpaca Webhook",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    log.warning("Invalid payload", extra={"errors": exc.errors()})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "Invalid payload", "detail": exc.errors()},
    )


# ── Request models ────────────────────────────────────────────────────────────

class _RobinhoodAuthRequest(BaseModel):
    secret: str
    sms_code: str


class _PickleUploadRequest(BaseModel):
    secret: str
    pickle_b64: str


class _ClaudeSignalRequest(BaseModel):
    secret: str
    ticker: str
    action: str  # "BUY" or "SELL"
    tweet_url: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health():
    """Liveness / readiness probe. Returns 200 while the server is up."""
    return {
        "status":  "ok",
        "uptime_s": round(time.time() - _start_time, 1),
        "paper":   "paper" in settings.alpaca_base_url,
    }


@app.get("/healthz", tags=["ops"])
async def healthz():
    """Deep health check — verifies Alpaca API connectivity and RH session.
    Always returns 200; check 'status' field for component health.
    """
    loop = asyncio.get_running_loop()

    alpaca_status = "up"
    alpaca_error: Optional[str] = None
    try:
        await loop.run_in_executor(None, get_account)
    except Exception as exc:
        alpaca_status = "down"
        alpaca_error = str(exc)[:200]

    rh_status = "active" if rh_client.available else "offline"
    overall = "healthy" if alpaca_status == "up" else "degraded"

    return {
        "status":       overall,
        "alpaca":       alpaca_status,
        "alpaca_error": alpaca_error,
        "robinhood":    rh_status,
        "rh_enabled":   settings.rh_enabled,
        "uptime_s":     round(time.time() - _start_time, 1),
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }


@app.post("/interactions", tags=["discord"])
async def interactions(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")

    if not settings.discord_app_public_key or not verify_discord_signature(
        settings.discord_app_public_key, signature, timestamp, body
    ):
        return JSONResponse(status_code=401, content={"error": "Invalid signature"})

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    if data.get("type") == 1:
        return {"type": 1}

    user_id = extract_user_id(data)
    if user_id != settings.discord_your_user_id:
        return {"type": 4, "data": {"content": "Unauthorized.", "flags": 64}}

    token = data["token"]
    command = data["data"]["name"]
    options = parse_options(data["data"].get("options", []))

    background_tasks.add_task(dispatch_command, command, options, token)
    return {"type": 5, "data": {"flags": 64}}


@app.post("/robinhood-auth", tags=["trading"])
async def robinhood_auth(body: _RobinhoodAuthRequest):
    """Re-authenticate Robinhood session using an SMS 2FA code."""
    try:
        verify_webhook_secret(body.secret)
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "Unauthorized."},
        )
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, rh_client.login_with_sms, body.sms_code)
        await notify_rh_session(
            "🔐 **ROBINHOOD SESSION RESTORED**\n"
            "Authenticated via SMS — session is live and trading resumes."
        )
        log.info("Robinhood session re-authenticated successfully")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "authenticated"},
        )
    except Exception as exc:
        log.warning("Robinhood re-auth failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Invalid SMS code or credentials."},
        )


_PICKLE_MAX_BYTES = 512 * 1024  # 512 KB — a real RH pickle is ~10 KB
# Valid Python 3 pickle protocol header bytes (protocols 2–5)
_PICKLE_MAGIC = {b"\x80\x02", b"\x80\x03", b"\x80\x04", b"\x80\x05"}


@app.post("/robinhood-upload-pickle", tags=["trading"])
async def robinhood_upload_pickle(body: _PickleUploadRequest):
    """Upload a locally-generated Robinhood pickle file to activate the session."""
    try:
        verify_webhook_secret(body.secret)
    except Exception:
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"error": "Unauthorized."})
    try:
        data = base64.b64decode(body.pickle_b64)
    except Exception:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": "Invalid base64 encoding."})

    # ── Security checks before touching disk ─────────────────────────────────
    if len(data) > _PICKLE_MAX_BYTES:
        log.warning("Pickle upload rejected — size %d bytes exceeds limit", len(data))
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"detail": f"File too large ({len(data):,} bytes). Maximum is {_PICKLE_MAX_BYTES:,} bytes."},
        )

    if len(data) < 2 or data[:2] not in _PICKLE_MAGIC:
        log.warning("Pickle upload rejected — invalid magic bytes: %s", data[:2].hex() if data else "empty")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "Invalid file — does not appear to be a valid Python pickle."},
        )

    try:
        from app.trading.robinhood_client import _PICKLE_BACKUP, _PICKLE_PATH, _TOKENS_DIR
        os.makedirs("/data", exist_ok=True)
        os.makedirs(_TOKENS_DIR, exist_ok=True)
        with open(_PICKLE_BACKUP, "wb") as f:
            f.write(data)
        with open(_PICKLE_PATH, "wb") as f:
            f.write(data)
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, rh_client.login_from_pickle):
            await notify_rh_session(
                "📦 **ROBINHOOD SESSION RESTORED**\n"
                "Session activated via pickle upload — ready to trade."
            )
            log.info("Robinhood session activated via pickle upload")
            return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "authenticated"})
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": "Pickle uploaded but session invalid — regenerate it."})
    except Exception as exc:
        log.warning("Pickle upload failed: %s", exc)
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


@app.post("/claude-signal", tags=["trading"])
async def claude_signal(body: _ClaudeSignalRequest):
    """
    Manually trigger a Claude portfolio trade from @theaiportfolios Twitter signal.

    Body: {"secret": "...", "ticker": "MSFT", "action": "BUY"|"SELL", "tweet_url": "..."}

    Executes on Robinhood using CLAUDE_LEVERAGE_FACTOR sizing (separate from Kimi trades),
    records in claude_portfolio.json, and notifies CLAUDE_PORTFOLIO_WEBHOOK_URL.
    """
    try:
        verify_webhook_secret(body.secret)
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "Unauthorized."},
        )

    action = body.action.upper()
    ticker = body.ticker.upper()
    if action not in ("BUY", "SELL"):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "action must be BUY or SELL"},
        )

    rh_result = await rh_client.execute_for_claude(action, ticker)

    from app.claude_portfolio import open_position, close_position, get_record
    from app.notifications import notify_claude_portfolio
    import pytz

    CT = pytz.timezone("America/Chicago")
    now = datetime.now(CT)
    hour = int(now.strftime("%I"))
    time_str = f"{hour}:{now.strftime('%M %p')} {now.strftime('%Z')} — {now.strftime('%B')} {now.day}, {now.year}"

    rh_status = rh_result.get("status")

    if rh_status == "failed":
        reason = rh_result.get("reason", "unknown")
        await notify_claude_portfolio(f"❌ 🤖 CLAUDE {action} {ticker} FAILED: {reason}")
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"status": "failed", "reason": reason},
        )

    if rh_status == "skipped":
        log.info("Claude signal skipped: %s", rh_result.get("reason"))
        return JSONResponse(status_code=status.HTTP_200_OK, content=rh_result)

    note = rh_result.get("note")
    if note:
        await notify_claude_portfolio(f"ℹ️ 🤖 CLAUDE {action} {ticker}: {note}")
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok", "note": note})

    queued = rh_result.get("queued", False)
    qty: float = rh_result.get("qty", 0)

    if action == "BUY":
        fill_price = rh_result.get("fill_price") or rh_result.get("price_est")
        if fill_price:
            open_position(ticker, qty, fill_price, body.tweet_url)

        if queued:
            price_str = f"≈${fill_price:,.2f}" if fill_price else "unknown"
            lines = [
                f"⏳ 🤖 **CLAUDE BUY — {ticker}**",
                f"Qty: {qty:g} shares queued for next market open @ {price_str}",
                f"🕐 {time_str}",
            ]
        else:
            price_str = f"${fill_price:,.2f}" if fill_price else "unknown"
            lines = [
                f"🟢 🤖 **CLAUDE BUY — {ticker}**",
                f"Qty: {qty:g} shares @ {price_str}",
                f"🕐 {time_str}",
            ]
        if body.tweet_url:
            lines.append(f"📌 {body.tweet_url}")
        await notify_claude_portfolio("\n".join(lines))

    else:  # SELL
        fill_price = rh_result.get("fill_price") or rh_result.get("price_est")
        from app.claude_portfolio import get_position as _get_claude_pos
        pre_pos = _get_claude_pos(ticker)
        entry_price_for_pending = pre_pos["entry_price"] if pre_pos else 0.0
        sold_qty, dollar_pnl, pct_pnl = close_position(ticker, fill_price or 0.0, body.tweet_url)

        if queued and rh_result.get("order_id"):
            from app.pending_orders import save_pending_order
            from app.trading.alpaca_client import get_next_trading_day
            from app.scheduler import scheduler
            from app.claude_manager import notify_claude_pending_sell_fill
            import pytz as _pytz
            from datetime import time as _dtime
            _et = _pytz.timezone("America/New_York")
            next_day = get_next_trading_day()
            run_dt = _et.localize(datetime.combine(next_day, _dtime(9, 31)))
            scheduler.add_job(
                notify_claude_pending_sell_fill, "date", run_date=run_dt,
                args=[rh_result["order_id"], ticker, entry_price_for_pending, sold_qty or 0.0, "autopilot"],
                id=f"pending_{rh_result['order_id']}", replace_existing=True,
            )
            save_pending_order(
                rh_result["order_id"], ticker, "SELL", fill_price, entry_price_for_pending,
                run_dt.isoformat(), broker="claude_sell",
                qty=sold_qty or 0.0, source="autopilot",
            )
        elif dollar_pnl is not None:
            from app.rh_trade_record import record_rh_trade
            await record_rh_trade(dollar_pnl >= 0, ticker, dollar_pnl)

        wins, losses = get_record()
        record_str = f"{wins}W - {losses}L"

        if queued:
            price_str = f"≈${fill_price:,.2f}" if fill_price else "unknown"
            lines = [
                f"⏳ 🤖 **CLAUDE SELL — {ticker}**",
                f"Qty: {qty:g} shares queued for next market open @ {price_str}",
                f"🕐 {time_str}",
            ]
        else:
            price_str = f"${fill_price:,.2f}" if fill_price else "unknown"
            lines = [
                f"🔴 🤖 **CLAUDE SELL — {ticker}**",
                f"Qty: {qty:g} shares @ {price_str}",
            ]
            if dollar_pnl is not None and pct_pnl is not None:
                if dollar_pnl >= 0:
                    lines.append(f"P&L: +${dollar_pnl:,.2f} (+{pct_pnl:.2f}%) 🟢 WIN")
                else:
                    lines.append(f"P&L: -${abs(dollar_pnl):,.2f} (-{abs(pct_pnl):.2f}%) 🔴 LOSS")
            lines.append(f"Claude Record: {record_str}")
            lines.append(f"🕐 {time_str}")
        if body.tweet_url:
            lines.append(f"📌 {body.tweet_url}")
        await notify_claude_portfolio("\n".join(lines))

    log.info("Claude signal processed: %s %s (queued=%s)", action, ticker, queued)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "result": rh_result},
    )


@app.post("/webhook", tags=["trading"])
async def webhook(request: Request):
    """
    Main TradingView alert receiver.

    Flow:
      1. Parse raw JSON (surface parse errors early).
      2. Validate secret.
      3. Parse + validate the full AlertPayload.
      4. Reject duplicates.
      5. Execute trading action via Alpaca.
      6. Return structured response.
    """
    # ── 1. Raw JSON parse ─────────────────────────────────────────────────────
    try:
        raw = await request.json()
    except Exception:
        log.warning("Received non-JSON request body")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Request body must be valid JSON."},
        )

    log.debug("Raw alert received", extra={"body": raw})

    # ── 2. Secret check ───────────────────────────────────────────────────────
    received_secret = raw.get("secret", "")
    try:
        verify_webhook_secret(received_secret)
    except Exception as exc:
        log.warning("Alert rejected — bad secret", extra={"ip": _client_ip(request)})
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "Unauthorized."},
        )

    # ── 3. Payload validation ─────────────────────────────────────────────────
    try:
        payload = AlertPayload(**raw)
    except ValidationError as exc:
        log.warning("Alert rejected — validation error", extra={"errors": exc.errors()})
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "Payload validation failed.", "detail": exc.errors()},
        )

    log.info(
        "Alert received",
        extra={
            "ticker":    payload.ticker,
            "action":    payload.action,
            "contracts": payload.contracts,
            "order_id":  payload.order_id,
            "timestamp": payload.timestamp,
        },
    )

    # ── 4. Idempotency check ──────────────────────────────────────────────────
    if is_duplicate(payload):
        log.info(
            "Duplicate alert ignored",
            extra={"ticker": payload.ticker, "order_id": payload.order_id},
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "duplicate", "message": "Alert already processed."},
        )

    # ── 5. Execute trade ──────────────────────────────────────────────────────
    try:
        avg_entry_price: Optional[float] = None
        if payload.action == TradingAction.REMOVE_LEVERAGE:
            # Use the stored ADD_LEVERAGE fill price, not the blended position
            # avg_entry_price. The blended average includes the base position's
            # (typically lower) cost basis and makes losing leverage trades
            # appear profitable.
            avg_entry_price = load_leverage_entry(payload.ticker)
        elif payload.action in _SELL_ACTIONS:
            pos = get_position(payload.ticker)
            if pos and pos.avg_entry_price:
                avg_entry_price = float(pos.avg_entry_price)

        result = await execute_action(payload)
        mark_processed(payload)

        log.info(
            "Trade executed",
            extra={"ticker": payload.ticker, "action": payload.action, "result": result},
        )

        await notify_trade(
            ticker=payload.ticker,
            action=payload.action.value.upper(),
            result=result,
            alert_price=payload.price,
            avg_entry_price=avg_entry_price,
        )

        await notify_rh_trade(
            ticker=payload.ticker,
            action=payload.action.value.upper(),
            rh_result=result.get("robinhood", {}),
            alert_price=payload.price,
        )

        if payload.action != TradingAction.BASE_ENTRY and result.get("orders"):
            _is_buy = payload.action not in _SELL_ACTIONS
            _emoji = "🟢" if _is_buy else "🔴"
            _side = "BUY" if _is_buy else "SELL"
            _price_str = f" @ ${payload.price:,.2f}" if payload.price else ""
            await notify_signal_feed(f"{_emoji} **{_side} {payload.ticker}**{_price_str}")

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "result": result},
        )

    except ValueError as exc:
        log.warning("Trade rejected — bad value: %s", exc, extra={"ticker": payload.ticker})
        await notify(f"⚠️ Trade rejected for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": str(exc)},
        )

    except APIError as exc:
        log.error(
            "Alpaca API error",
            exc_info=True,
            extra={"ticker": payload.ticker, "action": payload.action},
        )
        await notify(f"❌ Alpaca error for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "Alpaca API error.", "detail": str(exc)},
        )

    except Exception as exc:
        log.exception("Unexpected error processing alert")
        await notify(f"❌ Unexpected error for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error."},
        )


@app.post("/deposit", tags=["investors"])
async def deposit(request: Request) -> dict:
    """
    Record a cash deposit for an investor.

    Flow:
      1. Parse raw JSON and validate against DepositRequest.
      2. Verify webhook secret.
      3. Resolve SPY entry price (use provided value or fetch live from Alpaca).
      4. Append deposit to matching investor (case-insensitive), or create new one.
      5. Persist and return the updated investor record.
    """
    try:
        body = await request.json()
    except Exception:
        log.warning("Received non-JSON request body on /deposit")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Request body must be valid JSON."},
        )

    verify_webhook_secret(body.get("secret", ""))

    try:
        req = DepositRequest(**body)
    except ValidationError as exc:
        def _serialisable(errors):
            result = []
            for err in errors:
                err = dict(err)
                if "ctx" in err:
                    err["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
                err.pop("url", None)
                result.append(err)
            return result

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "Invalid deposit request.", "detail": _serialisable(exc.errors())},
        )

    spy_price = req.spy_price
    if spy_price is None:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            raise HTTPException(status_code=502, detail="Could not fetch current SPY price from Alpaca.")

    new_deposit = Deposit(amount=req.amount, entry_spy=spy_price, date=date.today().isoformat())

    async with investors_lock:
        investors = load_investors()
        match = next(
            (inv for inv in investors if inv.name.lower() == req.investor.lower()),
            None,
        )
        if match is None:
            match = Investor(name=req.investor, deposits=[new_deposit])
            investors.append(match)
        else:
            match.deposits.append(new_deposit)
        save_investors(investors)

    return {
        "investor": match.name,
        "deposits": [
            {"amount": d.amount, "entry_spy": d.entry_spy, "date": d.date}
            for d in match.deposits
        ],
    }


@app.post("/run-rebalance", tags=["trading"])
async def run_rebalance(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Manually trigger the Claude portfolio monthly rebalance. Body: {"secret": "..."}"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Request body must be valid JSON."})

    verify_webhook_secret(body.get("secret", ""))

    from app.claude_manager import run_monthly_rebalance
    background_tasks.add_task(run_monthly_rebalance)
    return {"status": "ok", "message": "Rebalance started in background — watch Discord for updates."}


@app.post("/run-report")
async def run_report(request: Request) -> dict:
    """Manually trigger a P&L report. Body: {"secret": "...", "report": "daily"|"weekly"|"monthly"|"ytd"|"1year"|"alltime"|"inception"|"custom"|"both"}

    "custom" additionally requires {"date": "YYYY-MM-DD"} for the start date.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Request body must be valid JSON."})

    verify_webhook_secret(body.get("secret", ""))

    _VALID = {"daily", "weekly", "monthly", "ytd", "1year", "alltime", "inception", "custom", "both", "investors"}
    report = body.get("report", "daily")
    if report not in _VALID:
        return JSONResponse(
            status_code=422,
            content={"error": f"report must be one of: {', '.join(sorted(_VALID))}"},
        )

    if report == "custom":
        date_str = body.get("date", "")
        try:
            custom_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={"error": "report 'custom' requires a 'date' field in YYYY-MM-DD format"},
            )
        if custom_date > datetime.now(ET).date():
            return JSONResponse(status_code=422, content={"error": "date cannot be in the future"})
        await send_custom_report(custom_date)
        return {"status": "ok", "report": report, "date": date_str}

    if report in ("daily", "both"):
        await send_daily_report()
    if report in ("weekly", "both"):
        await send_weekly_report()
    if report == "monthly":
        await send_monthly_report()
    if report == "ytd":
        await send_ytd_report()
    if report == "1year":
        await send_yearly_report()
    if report == "alltime":
        await send_alltime_report()
    if report == "inception":
        await send_inception_report()
    if report == "investors":
        await send_investor_report()

    return {"status": "ok", "report": report}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Best-effort client IP (respects X-Forwarded-For from proxies)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── Dev runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
        log_config=None,
    )
