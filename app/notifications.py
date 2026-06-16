"""
notifications.py — Optional Discord / Telegram alert stubs.

Set DISCORD_WEBHOOK_URL or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env
to enable. If the env vars are blank these functions are no-ops, so you
can safely call them regardless of whether notifications are configured.

Both functions are fire-and-forget — they log errors but never raise so
that a notification failure never blocks order execution.
"""

import json as _json
import logging
import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Persistent client — reuses connections across all notification calls.
# Closed in main.py lifespan on shutdown.
_client = httpx.AsyncClient()


def get_http_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient for reuse outside this module."""
    return _client


async def close_http_client() -> None:
    """Close the shared httpx client. Call once on app shutdown."""
    await _client.aclose()


async def notify(message: str) -> None:
    """Send a notification to all configured channels."""
    if settings.discord_webhook_url:
        await _discord(message)
    if settings.telegram_bot_token and settings.telegram_chat_id:
        await _telegram(message)


async def _discord(message: str) -> None:
    try:
        await _client.post(
            settings.discord_webhook_url,
            json={"content": message[:2000]},
            timeout=5,
        )
    except Exception as exc:
        log.warning("Discord notification failed: %s", exc)


async def notify_investors(message: str) -> None:
    url = settings.discord_investors_webhook_url or settings.discord_webhook_url
    if not url:
        log.warning("No Discord webhook configured for investor notifications; skipping")
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Investor Discord notification failed: %s", exc)


async def notify_investors_with_chart(message: str, chart_bytes: bytes) -> None:
    """Send the investor breakdown message with a PNG pie chart attachment.
    Uses DISCORD_INVESTORS_WEBHOOK_URL, falls back to the main channel.
    """
    url = settings.discord_investors_webhook_url or settings.discord_webhook_url
    if not url:
        log.warning("No Discord webhook configured for investor notifications; skipping")
        return
    try:
        if chart_bytes:
            await _client.post(
                url,
                data={"payload_json": _json.dumps({"content": message[:2000]})},
                files={"file": ("investors.png", chart_bytes, "image/png")},
                timeout=15,
            )
        else:
            await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Investor chart Discord notification failed: %s", exc)


async def notify_trades(message: str) -> None:
    url = settings.discord_trades_webhook_url
    if not url:
        log.warning("DISCORD_TRADES_WEBHOOK_URL not set; skipping trade notification")
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Trade Discord notification failed: %s", exc)


async def notify_signal_feed(message: str) -> None:
    """Post to the paid signal-subscriber Discord feed. No-op if unconfigured."""
    url = settings.signal_subscribers_webhook_url
    if not url:
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Signal feed Discord notification failed: %s", exc)


async def notify_claude_signal_feed(message: str) -> None:
    """Post to the paid Claude Manager subscriber Discord feed. No-op if unconfigured."""
    url = settings.claude_subscribers_webhook_url
    if not url:
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Claude signal feed Discord notification failed: %s", exc)


async def notify_with_chart(message: str, chart_bytes: bytes) -> None:
    """Send a Discord message with a PNG chart attachment to the main channel."""
    url = settings.discord_webhook_url
    if not url:
        log.warning("DISCORD_WEBHOOK_URL not set; skipping chart notification")
        return
    try:
        await _client.post(
            url,
            data={"payload_json": _json.dumps({"content": message[:2000]})},
            files={"file": ("chart.png", chart_bytes, "image/png")},
            timeout=15,
        )
    except Exception as exc:
        log.warning("Discord chart notification failed: %s", exc)


async def _telegram(message: str) -> None:
    url = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        f"/sendMessage"
    )
    try:
        await _client.post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": message[:4096],
                "parse_mode": "HTML",
            },
            timeout=5,
        )
    except Exception as exc:
        log.warning("Telegram notification failed: %s", exc)


async def notify_rh_session(message: str) -> None:
    """Send a Robinhood session status notification (refresh, expiry warnings).
    Uses RH_SESSION_WEBHOOK_URL, falls back to RH_DISCORD_WEBHOOK_URL, then main channel.
    Automatically appends a CT timestamp to every message.
    """
    from datetime import datetime
    import pytz
    _CT = pytz.timezone("America/Chicago")
    now = datetime.now(_CT)
    hour = int(now.strftime("%I"))
    ts = f"🕐 {hour}:{now.strftime('%M %p')} {now.strftime('%Z')} — {now.strftime('%A, %B')} {now.day}, {now.year}"
    full_message = f"{message}\n{ts}"

    url = settings.rh_session_webhook_url or settings.rh_discord_webhook_url or settings.discord_webhook_url
    if not url:
        return
    try:
        await _client.post(url, json={"content": full_message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Robinhood session notification failed: %s", exc)


async def notify_robinhood(message: str) -> None:
    """Send a notification to the Robinhood trades Discord channel.
    Falls back to the main Discord channel if RH_DISCORD_WEBHOOK_URL is not set.
    """
    url = settings.rh_discord_webhook_url or settings.discord_webhook_url
    if not url:
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Robinhood Discord notification failed: %s", exc)


async def notify_rh_pnl(message: str) -> None:
    """Send a Robinhood P&L report to RH_PNL_WEBHOOK_URL.
    Falls back to RH_DISCORD_WEBHOOK_URL, then main channel.
    """
    url = settings.rh_pnl_webhook_url or settings.rh_discord_webhook_url or settings.discord_webhook_url
    if not url:
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Robinhood P&L notification failed: %s", exc)


async def notify_alpaca_tax(message: str) -> None:
    """Send an Alpaca tax summary to ALPACA_TAX_WEBHOOK_URL, falls back to main channel."""
    url = settings.alpaca_tax_webhook_url or settings.discord_webhook_url
    if not url:
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Alpaca tax notification failed: %s", exc)


async def notify_claude_manager(message: str) -> None:
    """Send a Claude trade signal to CLAUDE_MANAGER_WEBHOOK_URL (trades channel).
    Falls back to RH channel, then main channel.
    """
    url = (
        settings.claude_manager_webhook_url
        or settings.rh_discord_webhook_url
        or settings.discord_webhook_url
    )
    if not url:
        return
    for chunk in _chunk(message, 1990):
        try:
            await _client.post(url, json={"content": chunk}, timeout=5)
        except Exception as exc:
            log.warning("Claude manager notification failed: %s", exc)


async def notify_claude_manager_with_chart(message: str, chart_bytes: bytes) -> None:
    """Send a financials chart PNG to CLAUDE_MANAGER_WEBHOOK_URL."""
    url = (
        settings.claude_manager_webhook_url
        or settings.rh_discord_webhook_url
        or settings.discord_webhook_url
    )
    if not url:
        return
    try:
        payload = {"content": message[:2000]} if message else {}
        await _client.post(
            url,
            data={"payload_json": _json.dumps(payload)},
            files={"file": ("financials.png", chart_bytes, "image/png")},
            timeout=15,
        )
    except Exception as exc:
        log.warning("Claude manager chart notification failed: %s", exc)



def _chunk(text: str, size: int) -> list[str]:
    """Split text into chunks that fit within Discord's message size limit."""
    return [text[i:i + size] for i in range(0, len(text), size)]


async def notify_claude_portfolio(message: str) -> None:
    """Send a Claude portfolio trade notification to CLAUDE_PORTFOLIO_WEBHOOK_URL.
    Falls back to RH channel, then main channel.
    """
    url = (
        settings.claude_portfolio_webhook_url
        or settings.rh_discord_webhook_url
        or settings.discord_webhook_url
    )
    if not url:
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Claude portfolio notification failed: %s", exc)


async def notify_portfolio_snapshot(message: str, chart_bytes: bytes) -> None:
    """Send the RH portfolio pie chart to PORTFOLIO_SNAPSHOT_WEBHOOK_URL."""
    url = settings.portfolio_snapshot_webhook_url or settings.rh_discord_webhook_url or settings.discord_webhook_url
    if not url:
        log.warning("No webhook configured for portfolio snapshot; skipping")
        return
    try:
        if chart_bytes:
            await _client.post(
                url,
                data={"payload_json": _json.dumps({"content": message[:2000]})},
                files={"file": ("portfolio.png", chart_bytes, "image/png")},
                timeout=15,
            )
        else:
            await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Portfolio snapshot notification failed: %s", exc)


async def notify_rh_tax(message: str) -> None:
    """Send a Robinhood tax summary to RH_TAX_WEBHOOK_URL, falls back to RH channel."""
    url = settings.rh_tax_webhook_url or settings.rh_discord_webhook_url or settings.discord_webhook_url
    if not url:
        return
    try:
        await _client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("RH tax notification failed: %s", exc)


async def notify_rh_pnl_with_chart(message: str, chart_bytes: bytes) -> None:
    """Send an RH P&L report with a PNG chart to RH_PNL_WEBHOOK_URL."""
    url = settings.rh_pnl_webhook_url or settings.rh_discord_webhook_url or settings.discord_webhook_url
    if not url:
        log.warning("No webhook configured for RH P&L chart; skipping")
        return
    try:
        await _client.post(
            url,
            data={"payload_json": _json.dumps({"content": message[:2000]})},
            files={"file": ("rh_pnl.png", chart_bytes, "image/png")},
            timeout=15,
        )
    except Exception as exc:
        log.warning("RH P&L chart notification failed: %s", exc)
