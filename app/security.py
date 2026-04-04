"""
security.py — Webhook authentication helpers.

We use a shared-secret strategy: TradingView embeds `"secret": "VALUE"` in
every alert JSON body. The value must match the WEBHOOK_SECRET env var.

Using hmac.compare_digest instead of == prevents timing-oracle attacks.
"""

import hmac
from fastapi import HTTPException, status
from app.config import settings


def verify_webhook_secret(received_secret: str) -> None:
    """
    Raise HTTP 401 if the secret in the payload does not match WEBHOOK_SECRET.

    Uses constant-time comparison to prevent timing attacks.
    """
    expected = settings.webhook_secret.encode()
    received = received_secret.encode()

    if not hmac.compare_digest(expected, received):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret.",
        )
