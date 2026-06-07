from __future__ import annotations

import logging
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger(__name__)


def verify_discord_signature(
    public_key_hex: str,
    signature_hex: str,
    timestamp: str,
    body: bytes,
) -> bool:
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
        signature_bytes = bytes.fromhex(signature_hex)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature_bytes, timestamp.encode() + body)
        return True
    except (InvalidSignature, Exception):
        return False


def extract_user_id(data: dict) -> Optional[str]:
    member = data.get("member", {})
    if member:
        return member.get("user", {}).get("id")
    return data.get("user", {}).get("id")


def parse_options(options: list) -> dict:
    """Parse Discord interaction options.

    For sub-commands (type=1), flattens the nested options into the result
    dict and adds "_subcommand" with the sub-command name so callers can
    branch on it without knowing the raw Discord structure.
    """
    if options and options[0].get("type") == 1:  # SUB_COMMAND
        sub = options[0]
        result = {"_subcommand": sub["name"]}
        result.update({opt["name"]: opt["value"] for opt in sub.get("options", [])})
        return result
    return {opt["name"]: opt["value"] for opt in options}
