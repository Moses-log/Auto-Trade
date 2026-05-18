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
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import settings
from app.github_commit import commit_investors_json
from app.idempotency import is_duplicate, mark_processed
from app.investors import Deposit, Investor, load_investors, save_investors, serialize_investors
from app.logging_config import setup_logging
from app.models import AlertPayload, DepositRequest, TradingAction
from app.notifications import notify
from app.pnl import (
    send_daily_report,
    send_weekly_report,
    send_monthly_report,
    send_yearly_report,
    send_ytd_report,
    send_alltime_report,
    send_investor_report,
)
from app.trade_notifier import notify_trade
from app.scheduler import scheduler, setup_jobs, reschedule_pending_orders
from app.security import verify_webhook_secret
from app.discord_commands import dispatch_command
from app.interactions import extract_user_id, parse_options, verify_discord_signature
from app.trading.alpaca_client import get_latest_price, get_position
from app.trading.order_logic import execute_action
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
    setup_jobs()
    reschedule_pending_orders()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health():
    """Liveness / readiness probe. Returns 200 while the server is up."""
    return {
        "status":  "ok",
        "uptime_s": round(time.time() - _start_time, 1),
        "paper":   "paper" in settings.alpaca_base_url,
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
        if payload.action in _SELL_ACTIONS:
            pos = get_position(payload.ticker)
            if pos and pos.avg_entry_price:
                avg_entry_price = float(pos.avg_entry_price)

        result = await execute_action(payload)
        mark_processed(payload)

        log.info(
            "Trade executed",
            extra={"ticker": payload.ticker, "action": payload.action, "result": result},
        )

        asyncio.create_task(
            notify_trade(
                ticker=payload.ticker,
                action=payload.action.value.upper(),
                result=result,
                alert_price=payload.price,
                avg_entry_price=avg_entry_price,
            )
        )

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

    investors = load_investors()
    match = next(
        (inv for inv in investors if inv.name.lower() == req.investor.lower()),
        None,
    )

    spy_price = req.spy_price
    if spy_price is None:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            raise HTTPException(status_code=502, detail="Could not fetch current SPY price from Alpaca.")

    new_deposit = Deposit(amount=req.amount, entry_spy=spy_price, date=date.today().isoformat())

    if match is None:
        match = Investor(name=req.investor, deposits=[new_deposit])
        investors.append(match)
    else:
        match.deposits.append(new_deposit)

    content = serialize_investors(investors)
    try:
        await commit_investors_json(content)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    save_investors(investors)

    return {
        "investor": match.name,
        "deposits": [
            {"amount": d.amount, "entry_spy": d.entry_spy, "date": d.date}
            for d in match.deposits
        ],
    }


@app.post("/run-report")
async def run_report(request: Request) -> dict:
    """Manually trigger a P&L report. Body: {"secret": "...", "report": "daily"|"weekly"|"monthly"|"ytd"|"1year"|"alltime"|"both"}"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Request body must be valid JSON."})

    verify_webhook_secret(body.get("secret", ""))

    _VALID = {"daily", "weekly", "monthly", "ytd", "1year", "alltime", "both", "investors"}
    report = body.get("report", "daily")
    if report not in _VALID:
        return JSONResponse(
            status_code=422,
            content={"error": f"report must be one of: {', '.join(sorted(_VALID))}"},
        )

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
