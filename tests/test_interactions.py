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
