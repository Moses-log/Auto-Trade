import os
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_private_key = Ed25519PrivateKey.generate()
_public_key = _private_key.public_key()
TEST_PUBLIC_KEY_HEX = _public_key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _sign(timestamp: str, body: bytes) -> str:
    return _private_key.sign(timestamp.encode() + body).hex()


def test_verify_valid_signature():
    from app.interactions import verify_discord_signature
    ts = "1234567890"
    body = b'{"type":1}'
    sig = _sign(ts, body)
    assert verify_discord_signature(TEST_PUBLIC_KEY_HEX, sig, ts, body) is True


def test_verify_invalid_signature():
    from app.interactions import verify_discord_signature
    assert verify_discord_signature(TEST_PUBLIC_KEY_HEX, "deadbeef", "ts", b"body") is False


def test_verify_wrong_body():
    from app.interactions import verify_discord_signature
    ts = "1234567890"
    body = b'{"type":1}'
    sig = _sign(ts, body)
    assert verify_discord_signature(TEST_PUBLIC_KEY_HEX, sig, ts, b"tampered") is False


def test_extract_user_id_from_guild_interaction():
    from app.interactions import extract_user_id
    data = {"member": {"user": {"id": "12345"}}}
    assert extract_user_id(data) == "12345"


def test_extract_user_id_from_dm_interaction():
    from app.interactions import extract_user_id
    data = {"user": {"id": "67890"}}
    assert extract_user_id(data) == "67890"


def test_extract_user_id_returns_none_when_missing():
    from app.interactions import extract_user_id
    assert extract_user_id({}) is None


def test_parse_options_returns_dict():
    from app.interactions import parse_options
    options = [
        {"name": "investor", "value": "Moses"},
        {"name": "amount", "value": 500.0},
    ]
    assert parse_options(options) == {"investor": "Moses", "amount": 500.0}


def test_parse_options_empty():
    from app.interactions import parse_options
    assert parse_options([]) == {}


_private_key2 = Ed25519PrivateKey.generate()
_public_key2 = _private_key2.public_key()
TEST_PK2 = _public_key2.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _sign2(timestamp: str, body: bytes) -> str:
    return _private_key2.sign(timestamp.encode() + body).hex()


@pytest.mark.asyncio
async def test_interactions_ping():
    with patch("app.config.settings.discord_app_public_key", TEST_PK2), \
         patch("app.config.settings.discord_your_user_id", "user123"):
        from app.main import app
        body = json.dumps({"type": 1}).encode()
        ts = "1234567890"
        sig = _sign2(ts, body)
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/interactions",
                content=body,
                headers={
                    "X-Signature-Ed25519": sig,
                    "X-Signature-Timestamp": ts,
                    "Content-Type": "application/json",
                },
            )
    assert resp.status_code == 200
    assert resp.json()["type"] == 1


@pytest.mark.asyncio
async def test_interactions_invalid_signature_returns_401():
    with patch("app.config.settings.discord_app_public_key", TEST_PK2):
        from app.main import app
        body = json.dumps({"type": 1}).encode()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/interactions",
                content=body,
                headers={
                    "X-Signature-Ed25519": "badbad",
                    "X-Signature-Timestamp": "ts",
                    "Content-Type": "application/json",
                },
            )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_interactions_unauthorized_user():
    with patch("app.config.settings.discord_app_public_key", TEST_PK2), \
         patch("app.config.settings.discord_your_user_id", "user123"):
        from app.main import app
        body = json.dumps({
            "type": 2,
            "token": "tok",
            "user": {"id": "wrong-user"},
            "data": {"name": "report", "options": [{"name": "type", "value": "daily"}]},
        }).encode()
        ts = "1234567890"
        sig = _sign2(ts, body)
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/interactions",
                content=body,
                headers={
                    "X-Signature-Ed25519": sig,
                    "X-Signature-Timestamp": ts,
                    "Content-Type": "application/json",
                },
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"]["content"] == "Unauthorized."
    assert data["data"]["flags"] == 64


@pytest.mark.asyncio
async def test_interactions_valid_command_dispatches():
    with patch("app.config.settings.discord_app_public_key", TEST_PK2), \
         patch("app.config.settings.discord_your_user_id", "user123"), \
         patch("app.discord_commands.dispatch_command", new_callable=AsyncMock) as mock_dispatch:
        from app.main import app
        body = json.dumps({
            "type": 2,
            "token": "real-token",
            "user": {"id": "user123"},
            "data": {
                "name": "report",
                "options": [{"name": "type", "value": "daily"}],
            },
        }).encode()
        ts = "1234567890"
        sig = _sign2(ts, body)
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/interactions",
                content=body,
                headers={
                    "X-Signature-Ed25519": sig,
                    "X-Signature-Timestamp": ts,
                    "Content-Type": "application/json",
                },
            )
    assert resp.status_code == 200
    assert resp.json()["type"] == 5
