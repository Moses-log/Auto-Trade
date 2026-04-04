"""
notifications.py — Optional Discord / Telegram alert stubs.

Set DISCORD_WEBHOOK_URL or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env
to enable. If the env vars are blank these functions are no-ops, so you
can safely call them regardless of whether notifications are configured.

Both functions are fire-and-forget — they log errors but never raise so
that a notification failure never blocks order execution.
"""

import logging
import httpx

from app.config import settings

log = logging.getLogger(__name__)


async def notify(message: str) -> None:
    """Send a notification to all configured channels."""
    if settings.discord_webhook_url:
        await _discord(message)
    if settings.telegram_bot_token and settings.telegram_chat_id:
        await _telegram(message)


async def _discord(message: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                settings.discord_webhook_url,
                json={"content": message[:2000]},  # Discord 2 000-char limit
            )
    except Exception as exc:
        log.warning("Discord notification failed: %s", exc)


async def _telegram(message: str) -> None:
    url = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        f"/sendMessage"
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": message[:4096],  # Telegram 4 096-char limit
                    "parse_mode": "HTML",
                },
            )
    except Exception as exc:
        log.warning("Telegram notification failed: %s", exc)
