# Discord Slash Commands — Design Spec

**Date:** 2026-05-16
**Feature:** Replace manual `POST /deposit` terminal calls with Discord slash commands

---

## Problem

Recording investor deposits, withdrawals, and triggering P&L reports requires manually calling HTTP endpoints from a terminal. Easy to forget, inconvenient on mobile, requires knowing the exact payload format.

---

## Goal

Three Discord slash commands usable directly from the Discord server — no terminal, no Postman, no curl.

---

## Commands

### `/deposit`
Records a cash deposit for an investor.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `investor` | string | yes | Investor name (case-insensitive) |
| `amount` | number | yes | Deposit amount in USD |
| `spy_price` | number | no | SPY entry price — fetched live from Alpaca if omitted |

Calls existing `POST /deposit` logic exactly. No new deposit logic written.

**Success response (ephemeral):**
```
✅ Moses — $2,000 deposit recorded
SPY entry: $741.20
```

---

### `/withdraw`
Records a cash withdrawal for an investor. Reduces their deposited total.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `investor` | string | yes | Investor name (case-insensitive) |
| `amount` | number | yes | Withdrawal amount in USD |

Adds a negative deposit entry at current SPY price:
```json
{"amount": -500, "entry_spy": 741.20, "date": "2026-05-16"}
```

Existing equity formula handles the negative term naturally — reduces equity by the withdrawal amount at that moment, remaining portfolio continues tracking SPY.

Commits updated `investors.json` to GitHub same as `/deposit`.

**Success response (ephemeral):**
```
✅ Moses — $500 withdrawal recorded
SPY @ $741.20
Remaining deposited: $1,500
```

---

### `/report`
Manually triggers a P&L report to Discord.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | `daily`, `weekly`, or `both` |

Calls existing report logic. Report fires to the normal Discord channel.

**Success response (ephemeral):**
```
✅ Daily report sent
```

---

## Architecture

```
User types /deposit Moses 2000  (in Discord)
          │
          ▼
Discord POSTs to POST /interactions
          │
          ▼
Verify Ed25519 signature
(X-Signature-Ed25519 + X-Signature-Timestamp headers + DISCORD_APP_PUBLIC_KEY)
→ 401 if invalid
          │
          ▼
Handle PING → return PONG  (Discord endpoint verification)
          │
          ▼
Check interaction.user.id == DISCORD_YOUR_USER_ID
→ ephemeral "Unauthorized" if not you
          │
          ▼
Defer response (instant ACK to Discord — avoids 3-second timeout)
Spin up background task for actual command logic
          │
          ▼
Background task runs → follow-up message sent to Discord when done
```

---

## Deferred Response Pattern

Discord requires a response within 3 seconds or shows "The application did not respond."

All commands use the deferred pattern:
1. Server immediately returns `{"type": 5}` (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE) — ACKs within 3 seconds
2. Background task runs the actual logic (deposit, withdraw, report)
3. Server POSTs the result to Discord's follow-up webhook URL

This means no 3-second pressure on any command, even if GitHub commit or Alpaca price fetch is slow.

All responses are **ephemeral** (only visible to you) except `/report` — the report itself fires to the public channel as normal.

---

## Security

**Layer 1 — Discord signature verification**
Every interaction is signed by Discord using the app's Ed25519 private key. Server verifies using `DISCORD_APP_PUBLIC_KEY`. Return 401 immediately if invalid. Implemented using Python's `cryptography` library (no new heavyweight dependency).

**Layer 2 — User ID allowlist**
After signature passes, `interaction.user.id` is checked against `DISCORD_YOUR_USER_ID`. If it doesn't match, respond with ephemeral "Unauthorized." Even if someone else is in the server, they cannot trigger commands.

---

## Error Handling

All errors returned as ephemeral messages (only you see them):

| Scenario | Response |
|---|---|
| Investor name not found | `❌ Investor "Dave" not found — check spelling` |
| Withdrawal exceeds total deposited | `❌ Withdrawal $2,500 exceeds Moses total $1,500` |
| Amount ≤ 0 | `❌ Amount must be positive` |
| Alpaca price fetch fails | `❌ Could not fetch SPY price — provide spy_price manually` |
| GitHub commit fails | `⚠️ Recorded locally but GitHub commit failed` |

---

## New Files

| File | Purpose |
|---|---|
| `app/interactions.py` | Signature verification, PING handler, user ID check, command routing |
| `app/discord_commands.py` | `/deposit`, `/withdraw`, `/report` handlers |
| `scripts/register_commands.py` | One-time script to register slash commands with Discord API |

---

## Changes to Existing Files

| File | Change |
|---|---|
| `app/main.py` | Add `POST /interactions` route |
| `app/investors.py` | Add `get_total_deposited(investor)` helper for withdrawal validation |
| `app/github_commit.py` | No change — existing `commit_investors_json` reused |

---

## New Environment Variables

| Variable | Where to get it |
|---|---|
| `DISCORD_APP_PUBLIC_KEY` | Discord Developer Portal → your app → General Information |
| `DISCORD_APP_ID` | Same page |
| `DISCORD_YOUR_USER_ID` | Discord → Settings → Advanced → Developer Mode → right-click your name → Copy User ID |

---

## One-Time Discord Setup

1. Discord Developer Portal → **New Application**
2. **Bot** tab → Add Bot
3. Copy **Public Key** + **Application ID** → add to Render env vars
4. Set **Interactions Endpoint URL** → `https://auto-trade-ro8k.onrender.com/interactions`
5. Run `python scripts/register_commands.py` to register the 3 commands
6. Copy your Discord **User ID** → add `DISCORD_YOUR_USER_ID` to Render env vars

---

## Out of Scope

- Tracking which specific SPY buy corresponds to which investor deposit (equity = deposit amount × SPY growth from deposit date)
- Multi-user access / role-based permissions
- Command history / audit log
- Editing or deleting past deposits
