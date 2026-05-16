# Discord Slash Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `/deposit`, `/withdraw`, and `/report` Discord slash commands so investor operations can be triggered from Discord instead of the terminal.

**Architecture:** Discord POSTs interactions to `POST /interactions` on the existing FastAPI server. The endpoint verifies the Ed25519 signature, checks the user ID, defers the response immediately (avoids the 3-second Discord timeout), and runs the command logic in a background task that edits the deferred message when done.

**Tech Stack:** FastAPI, httpx, cryptography (Ed25519), existing investors/deposit/report logic.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `app/config.py` | Modify | Add `discord_app_public_key`, `discord_app_id`, `discord_your_user_id` |
| `app/investors.py` | Modify | Add `get_total_deposited(inv)` helper |
| `app/interactions.py` | Create | Ed25519 verification, PING, user ID check, deferred routing |
| `app/discord_commands.py` | Create | `/deposit`, `/withdraw`, `/report` handlers + follow-up helper |
| `app/main.py` | Modify | Register `POST /interactions` route |
| `scripts/register_commands.py` | Create | One-time script to register commands with Discord API |
| `requirements.txt` | Modify | Add `cryptography>=42.0.0` |
| `.env.example` | Modify | Document new env vars |
| `tests/test_interactions.py` | Create | Signature verification + routing tests |
| `tests/test_discord_commands.py` | Create | Command handler tests |

---

## Task 1: Add Discord env vars to config

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add env vars to `app/config.py`**

Add after `discord_trades_webhook_url`:

```python
    # ── Discord slash commands ─────────────────────────────────────────────────
    discord_app_public_key: Optional[str] = None
    discord_app_id: Optional[str] = None
    discord_your_user_id: Optional[str] = None
```

- [ ] **Step 2: Update `.env.example`**

Add after the Discord section:

```
# ── Discord slash commands ────────────────────────────────────────────────────
# Get these from Discord Developer Portal → your app → General Information
DISCORD_APP_PUBLIC_KEY=
DISCORD_APP_ID=
# Your personal Discord user ID: Settings → Advanced → Developer Mode → right-click name → Copy User ID
DISCORD_YOUR_USER_ID=
```

- [ ] **Step 3: Commit**

```bash
git add app/config.py .env.example
git commit -m "chore(config): add Discord slash command env vars"
```

---

## Task 2: Add `get_total_deposited` helper to investors

**Files:**
- Modify: `app/investors.py`
- Test: `tests/test_investors.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_investors.py`:

```python
def test_get_total_deposited_sums_all_deposits():
    from app.investors import Investor, Deposit, get_total_deposited
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=707.0, date="2026-05-09"),
        Deposit(amount=200.0, entry_spy=720.0, date="2026-05-10"),
    ])
    assert get_total_deposited(inv) == 500.0


def test_get_total_deposited_handles_withdrawals():
    from app.investors import Investor, Deposit, get_total_deposited
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09"),
        Deposit(amount=-500.0, entry_spy=741.0, date="2026-05-16"),
    ])
    assert get_total_deposited(inv) == 1500.0
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_investors.py::test_get_total_deposited_sums_all_deposits -v
```
Expected: `ImportError` or `AttributeError` — `get_total_deposited` not defined.

- [ ] **Step 3: Add `get_total_deposited` to `app/investors.py`**

Add after the `save_investors` function:

```python
def get_total_deposited(investor: Investor) -> float:
    return sum(d.amount for d in investor.deposits)
```

- [ ] **Step 4: Run tests to verify they pass**

```
py -m pytest tests/test_investors.py::test_get_total_deposited_sums_all_deposits tests/test_investors.py::test_get_total_deposited_handles_withdrawals -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/investors.py tests/test_investors.py
git commit -m "feat(investors): add get_total_deposited helper"
```

---

## Task 3: Add cryptography dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add to `requirements.txt`**

Add after `tenacity`:

```
# Ed25519 signature verification for Discord interactions
cryptography>=42.0.0
```

- [ ] **Step 2: Install**

```
py -m pip install cryptography>=42.0.0
```
Expected: installs successfully (likely already present as a transitive dep).

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore(deps): add cryptography for Discord Ed25519 verification"
```

---

## Task 4: Create `app/interactions.py` — signature verification and routing

**Files:**
- Create: `app/interactions.py`
- Create: `tests/test_interactions.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_interactions.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_interactions.py -v
```
Expected: `ImportError` — `app.interactions` not found.

- [ ] **Step 3: Create `app/interactions.py`**

```python
from __future__ import annotations

import logging
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

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
    return {opt["name"]: opt["value"] for opt in options}
```

- [ ] **Step 4: Run tests to verify they pass**

```
py -m pytest tests/test_interactions.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/interactions.py tests/test_interactions.py
git commit -m "feat(interactions): add Discord signature verification and helpers"
```

---

## Task 5: Create `app/discord_commands.py` — command handlers

**Files:**
- Create: `app/discord_commands.py`
- Create: `tests/test_discord_commands.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_discord_commands.py`:

```python
import os
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")
os.environ.setdefault("DISCORD_APP_ID", "test-app-id")


@pytest.mark.asyncio
async def test_handle_deposit_success():
    fake_investor = MagicMock()
    fake_investor.name = "Moses"
    fake_investor.deposits = []

    with patch("app.discord_commands.load_investors", return_value=[fake_investor]), \
         patch("app.discord_commands.get_latest_price", return_value=741.20), \
         patch("app.discord_commands.commit_investors_json", new_callable=AsyncMock), \
         patch("app.discord_commands.save_investors"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_deposit
        await handle_deposit("Moses", 2000.0, None, "test-token")

    mock_edit.assert_called_once()
    msg = mock_edit.call_args[0][1]
    assert "Moses" in msg
    assert "2,000" in msg
    assert "741.20" in msg


@pytest.mark.asyncio
async def test_handle_deposit_investor_not_found():
    with patch("app.discord_commands.load_investors", return_value=[]), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_deposit
        await handle_deposit("Ghost", 500.0, None, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "not found" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_success():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.discord_commands.load_investors", return_value=[inv]), \
         patch("app.discord_commands.get_latest_price", return_value=741.20), \
         patch("app.discord_commands.commit_investors_json", new_callable=AsyncMock), \
         patch("app.discord_commands.save_investors"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "500" in msg
    assert "Moses" in msg
    assert "1,500" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_exceeds_total():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.discord_commands.load_investors", return_value=[inv]), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "exceeds" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_investor_not_found():
    with patch("app.discord_commands.load_investors", return_value=[]), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Ghost", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "not found" in msg


@pytest.mark.asyncio
async def test_handle_report_daily():
    with patch("app.discord_commands.send_daily_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("daily", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "daily" in msg.lower()


@pytest.mark.asyncio
async def test_handle_report_both():
    with patch("app.discord_commands.send_daily_report", new_callable=AsyncMock), \
         patch("app.discord_commands.send_weekly_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("both", "test-token")

    msg = mock_edit.call_args[0][1]
    assert "✅" in msg
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_discord_commands.py -v
```
Expected: `ImportError` — `app.discord_commands` not found.

- [ ] **Step 3: Create `app/discord_commands.py`**

```python
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import httpx

from app.config import settings
from app.github_commit import commit_investors_json
from app.investors import Deposit, get_total_deposited, load_investors, save_investors, serialize_investors
from app.pnl import send_daily_report, send_weekly_report
from app.trading.alpaca_client import get_latest_price

log = logging.getLogger(__name__)


async def _edit_original(token: str, content: str) -> None:
    url = f"https://discord.com/api/v10/webhooks/{settings.discord_app_id}/{token}/messages/@original"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.patch(url, json={"content": content})
    except Exception as exc:
        log.warning("Failed to edit Discord follow-up: %s", exc)


async def handle_deposit(
    investor_name: str,
    amount: float,
    spy_price: Optional[float],
    token: str,
) -> None:
    investors = load_investors()
    match = next((inv for inv in investors if inv.name.lower() == investor_name.lower()), None)

    if match is None:
        await _edit_original(token, f'❌ Investor "{investor_name}" not found — check spelling')
        return

    if spy_price is None:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            await _edit_original(token, "❌ Could not fetch SPY price — provide spy_price manually")
            return

    match.deposits.append(Deposit(amount=amount, entry_spy=spy_price, date=date.today().isoformat()))
    content = serialize_investors(investors)

    try:
        await commit_investors_json(content)
    except Exception as exc:
        log.warning("GitHub commit failed: %s", exc)
        save_investors(investors)
        await _edit_original(token, f"⚠️ {match.name} — ${amount:,.2f} deposit recorded locally but GitHub commit failed\nSPY entry: ${spy_price:,.2f}")
        return

    save_investors(investors)
    await _edit_original(token, f"✅ {match.name} — ${amount:,.2f} deposit recorded\nSPY entry: ${spy_price:,.2f}")


async def handle_withdraw(investor_name: str, amount: float, token: str) -> None:
    investors = load_investors()
    match = next((inv for inv in investors if inv.name.lower() == investor_name.lower()), None)

    if match is None:
        await _edit_original(token, f'❌ Investor "{investor_name}" not found — check spelling')
        return

    total = get_total_deposited(match)
    if amount > total:
        await _edit_original(
            token,
            f"❌ Withdrawal ${amount:,.2f} exceeds {match.name} total ${total:,.2f}"
        )
        return

    spy_price = get_latest_price("SPY")
    if spy_price is None:
        await _edit_original(token, "❌ Could not fetch SPY price — try again")
        return

    match.deposits.append(Deposit(amount=-amount, entry_spy=spy_price, date=date.today().isoformat()))
    content = serialize_investors(investors)

    try:
        await commit_investors_json(content)
    except Exception as exc:
        log.warning("GitHub commit failed: %s", exc)
        save_investors(investors)
        remaining = get_total_deposited(match)
        await _edit_original(token, f"⚠️ {match.name} — ${amount:,.2f} withdrawal recorded locally but GitHub commit failed\nSPY @ ${spy_price:,.2f}\nRemaining deposited: ${remaining:,.2f}")
        return

    save_investors(investors)
    remaining = get_total_deposited(match)
    await _edit_original(token, f"✅ {match.name} — ${amount:,.2f} withdrawal recorded\nSPY @ ${spy_price:,.2f}\nRemaining deposited: ${remaining:,.2f}")


async def handle_report(report_type: str, token: str) -> None:
    if report_type in ("daily", "both"):
        await send_daily_report()
    if report_type in ("weekly", "both"):
        await send_weekly_report()
    await _edit_original(token, f"✅ {report_type.capitalize()} report sent")


async def dispatch_command(command: str, options: dict, token: str) -> None:
    try:
        if command == "deposit":
            await handle_deposit(
                investor_name=options["investor"],
                amount=float(options["amount"]),
                spy_price=float(options["spy_price"]) if "spy_price" in options else None,
                token=token,
            )
        elif command == "withdraw":
            await handle_withdraw(
                investor_name=options["investor"],
                amount=float(options["amount"]),
                token=token,
            )
        elif command == "report":
            await handle_report(report_type=options["type"], token=token)
        else:
            await _edit_original(token, f"❌ Unknown command: {command}")
    except Exception as exc:
        log.exception("Command %s failed", command)
        await _edit_original(token, f"❌ Command failed: {exc}")
```

- [ ] **Step 4: Run tests to verify they pass**

```
py -m pytest tests/test_discord_commands.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/discord_commands.py tests/test_discord_commands.py
git commit -m "feat(discord): add deposit, withdraw, report command handlers"
```

---

## Task 6: Add `POST /interactions` route to `app/main.py`

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_interactions.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_interactions.py`:

```python
import json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from httpx import AsyncClient, ASGITransport

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
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_interactions.py::test_interactions_ping -v
```
Expected: FAIL — no `/interactions` route.

- [ ] **Step 3: Add route to `app/main.py`**

Add imports near the top of `app/main.py`:

```python
from app.interactions import extract_user_id, parse_options, verify_discord_signature
from app.discord_commands import dispatch_command
```

Add the route after the `/health` route:

```python
@app.post("/interactions", tags=["discord"])
async def interactions(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")

    if not settings.discord_app_public_key or not verify_discord_signature(
        settings.discord_app_public_key, signature, timestamp, body
    ):
        return JSONResponse(status_code=401, content={"error": "Invalid signature"})

    data = json.loads(body)

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
```

Also add `import json` and `from fastapi import BackgroundTasks` to the imports in `app/main.py` if not already present.

- [ ] **Step 4: Run tests to verify they pass**

```
py -m pytest tests/test_interactions.py -v
```
Expected: all pass.

- [ ] **Step 5: Run full test suite**

```
py -m pytest tests/ -v
```
Expected: all pass (1 pre-existing failure in `test_github_commit.py::test_commit_raises_on_get_failure` is unrelated — ignore it).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_interactions.py
git commit -m "feat(api): add POST /interactions endpoint for Discord slash commands"
```

---

## Task 7: Create `scripts/register_commands.py`

**Files:**
- Create: `scripts/register_commands.py`

This is a one-time script. Run it once after deploying to register the 3 commands with Discord.

- [ ] **Step 1: Create `scripts/register_commands.py`**

```python
"""
One-time script to register Discord slash commands.

Run once after setting DISCORD_APP_ID and DISCORD_BOT_TOKEN:
    DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... py scripts/register_commands.py
"""
import httpx
import os
import sys

APP_ID = os.environ.get("DISCORD_APP_ID")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

if not APP_ID or not BOT_TOKEN:
    print("ERROR: Set DISCORD_APP_ID and DISCORD_BOT_TOKEN env vars")
    sys.exit(1)

COMMANDS = [
    {
        "name": "deposit",
        "description": "Record an investor cash deposit",
        "options": [
            {
                "name": "investor",
                "description": "Investor name",
                "type": 3,
                "required": True,
            },
            {
                "name": "amount",
                "description": "Deposit amount in USD",
                "type": 10,
                "required": True,
            },
            {
                "name": "spy_price",
                "description": "SPY entry price (fetched live if omitted)",
                "type": 10,
                "required": False,
            },
        ],
    },
    {
        "name": "withdraw",
        "description": "Record an investor cash withdrawal",
        "options": [
            {
                "name": "investor",
                "description": "Investor name",
                "type": 3,
                "required": True,
            },
            {
                "name": "amount",
                "description": "Withdrawal amount in USD",
                "type": 10,
                "required": True,
            },
        ],
    },
    {
        "name": "report",
        "description": "Trigger a P&L report to Discord",
        "options": [
            {
                "name": "type",
                "description": "Report type",
                "type": 3,
                "required": True,
                "choices": [
                    {"name": "Daily", "value": "daily"},
                    {"name": "Weekly", "value": "weekly"},
                    {"name": "Both", "value": "both"},
                ],
            }
        ],
    },
]

url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
headers = {"Authorization": f"Bot {BOT_TOKEN}"}

with httpx.Client() as client:
    resp = client.put(url, json=COMMANDS, headers=headers, timeout=15)

if resp.status_code in (200, 201):
    print(f"✅ Registered {len(COMMANDS)} commands successfully")
    for cmd in resp.json():
        print(f"  /{cmd['name']}")
else:
    print(f"❌ Failed: {resp.status_code} {resp.text}")
    sys.exit(1)
```

- [ ] **Step 2: Commit**

```bash
git add scripts/register_commands.py
git commit -m "chore: add Discord slash command registration script"
```

---

## Task 8: Push and deploy

- [ ] **Step 1: Push to GitHub**

```bash
git push
```

- [ ] **Step 2: Add env vars in Render dashboard**

Go to Render → your service → Environment tab → add:
- `DISCORD_APP_PUBLIC_KEY` — from Discord Developer Portal → app → General Information
- `DISCORD_APP_ID` — same page
- `DISCORD_YOUR_USER_ID` — Discord Settings → Advanced → Developer Mode on → right-click your username → Copy User ID

- [ ] **Step 3: Discord Developer Portal setup**

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → give it a name (e.g. "Auto-Trade")
3. **General Information** tab → copy **Application ID** and **Public Key**
4. **Bot** tab → **Add Bot** → copy the bot token
5. **Installation** tab → set **Interactions Endpoint URL** to `https://auto-trade-ro8k.onrender.com/interactions`
   - Discord will send a PING to verify — server must be deployed first
6. Add the bot to your Discord server via OAuth2 → URL Generator → scope `applications.commands`

- [ ] **Step 4: Register commands**

After deploy is live, run locally (or in any terminal with the env vars):

```bash
DISCORD_APP_ID=your_app_id DISCORD_BOT_TOKEN=your_bot_token py scripts/register_commands.py
```
Expected:
```
✅ Registered 3 commands successfully
  /deposit
  /withdraw
  /report
```

- [ ] **Step 5: Test in Discord**

Type `/deposit` in your Discord server — Discord should show the autocomplete options. Submit a test deposit and verify the ✅ confirmation appears and `investors.json` is updated.

---

## Self-Review

**Spec coverage check:**
- ✅ `/deposit` command — Task 5
- ✅ `/withdraw` command — Task 5
- ✅ `/report` command — Task 5
- ✅ Ed25519 signature verification — Task 4
- ✅ User ID allowlist — Task 6
- ✅ Deferred response (3-second timeout safety) — Task 6
- ✅ `spy_price` optional on `/deposit` — Task 5
- ✅ Withdrawal validation (exceeds total) — Task 5
- ✅ Ephemeral error responses — Task 5
- ✅ One-time command registration script — Task 7
- ✅ New env vars documented — Tasks 1 & 8

**Type consistency check:**
- `verify_discord_signature(public_key_hex, signature_hex, timestamp, body)` — consistent Tasks 4 & 6
- `extract_user_id(data)` → `Optional[str]` — consistent Tasks 4 & 6
- `parse_options(options)` → `dict` — consistent Tasks 4 & 6
- `dispatch_command(command, options, token)` — consistent Tasks 5 & 6
- `handle_deposit(investor_name, amount, spy_price, token)` — consistent Tasks 5 & 6
- `handle_withdraw(investor_name, amount, token)` — consistent Tasks 5 & 6
- `handle_report(report_type, token)` — consistent Tasks 5 & 6
- `_edit_original(token, content)` — consistent throughout Task 5
- `get_total_deposited(investor)` → `float` — consistent Tasks 2 & 5
