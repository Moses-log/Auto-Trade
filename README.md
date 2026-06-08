# Kimi Auto Trade

A production-ready Python / FastAPI service that receives TradingView strategy alerts and executes trades simultaneously on **Alpaca** and **Robinhood**. Tracks a shared investor portfolio, posts detailed trade notifications and P&L reports to Discord, and handles queued/after-hours orders automatically.

Deployed on [Render](https://render.com) with a persistent disk for session state and data files.

---

## What is the Kimi Strategy?

Kimi is a dollar-cost-averaging (DCA) overlay strategy built on top of any base TradingView entry signal. The core idea:

1. **Base Entry** — you place the initial position manually on Alpaca (the bot intentionally ignores `base_entry` signals, letting you control the first entry).
2. **Add Leverage** — when the strategy signals a DCA add, the bot queries your real-time Alpaca buying power, calculates a buy quantity based on `leverage_factor`, and places the order automatically.
3. **Remove Leverage** — closes only the DCA portion of the position (calculated from `leverage_factor`), leaving the base position untouched.
4. **Stop Loss** — closes the entire position on Alpaca and sells all shares on Robinhood.

Robinhood mirrors every action (buy/sell/close) using fractional shares sized to `RH_LEVERAGE_FACTOR` × available buying power.

---

## System Architecture

```
TradingView Alert (Pine Script strategy.alert())
         │
         ▼ POST /webhook
   Render (FastAPI)
         │
         ├─ 1. Validate shared secret (constant-time)
         ├─ 2. Deduplicate (disk-backed TTL store, 5 min default)
         ├─ 3. Execute on Alpaca + Robinhood in sequence
         │      ├── Alpaca: market order via alpaca-py
         │      └── Robinhood: fractional order via robin_stocks
         │
         └─ 4. Notify Discord
                ├── Trades channel:    fill price, qty, position, P&L, win/loss record
                ├── Robinhood channel: RH fill, qty, position, P&L, RH win/loss record
                └── Main channel:      errors and scheduled reports

APScheduler (runs inside the Render process)
         ├── 4:00 PM ET  Mon–Thu  → Alpaca daily P&L + investor breakdown + period checks
         │                           RH daily P&L + RH period checks (all in parallel)
         ├── 4:00 PM ET  Friday   → Above + Alpaca/RH weekly P&L reports (all in parallel)
         ├── Every 72 hours       → Robinhood session keep-alive (silent token refresh)
         ├── Jan/Apr/Jul/Oct 1    → Quarterly Alpaca + RH tax summary reports
         └── Per pending order    → At 9:31 AM ET next trading day, poll for fill + notify

Persistent Disk (/data on Render)
         ├── investors.json        → Investor deposit records
         ├── pending_orders.json   → Queued Alpaca orders awaiting next market open
         ├── trade_record.json     → Alpaca win/loss record
         ├── rh_trade_record.json  → Robinhood win/loss record (separate from Alpaca)
         ├── leverage_entry.json   → ADD_LEVERAGE fill price per ticker (for accurate P&L)
         ├── idempotency.json      → Seen alert IDs for duplicate suppression
         └── robinhood.pickle      → Robinhood session token (auto-refreshed every 3 days)
```

---

## Project Structure

```
app/
├── main.py               # FastAPI app — all HTTP endpoints, app lifespan
├── config.py             # All settings via pydantic-settings (env vars / .env file)
├── models.py             # Pydantic models: AlertPayload, DepositRequest, TradingAction enum
├── security.py           # Shared-secret webhook validation (constant-time hmac.compare_digest)
├── idempotency.py        # Duplicate-alert suppression — disk-backed TTL store
├── logging_config.py     # Structured JSON logging setup
│
├── notifications.py      # Discord + Telegram notification helpers
│                         # Channels: main, investors, trades, robinhood, rh_session, rh_pnl
├── trade_notifier.py     # Alpaca trade notification + queued order scheduling
├── rh_trade_notifier.py  # Robinhood trade notification
│
├── pnl.py                # Alpaca P&L engine (daily/weekly/monthly/yearly/YTD/all-time)
├── rh_pnl.py             # Robinhood P&L engine (daily/weekly/monthly/yearly)
├── chart.py              # Portfolio vs SPY % return equity curve (PNG via matplotlib)
│
├── investors.py          # Investor data model, equity math, Discord report formatting
├── trade_record.py       # Alpaca win/loss counter — disk-backed, updated on every sell
├── rh_trade_record.py    # Robinhood win/loss counter — separate from Alpaca
├── leverage_state.py     # Stores ADD_LEVERAGE fill price per ticker for P&L accuracy
├── pending_orders.py     # Persist queued Alpaca orders to disk, survive restarts
├── interactions.py       # Discord Ed25519 signature verification + option parsing
├── discord_commands.py   # /deposit, /withdraw, /report slash command handlers
├── tax.py                # Quarterly Alpaca + RH tax summary reports
├── scheduler.py          # APScheduler job registration (P&L, keep-alive, tax reports)
│
└── trading/
    ├── alpaca_client.py    # Alpaca SDK wrapper: orders, positions, portfolio, market clock
    ├── order_logic.py      # TradingView action → Alpaca + Robinhood execution
    └── robinhood_client.py # Robinhood auth, session management, fractional order execution

tests/
├── conftest.py             # Shared fixtures — fake env vars, idempotency store isolation
├── test_webhook.py
├── test_robinhood_client.py
├── test_config_rh.py
├── test_notifications_rh.py
├── test_deposit.py
├── test_investors.py
├── test_pnl.py
├── test_trade_notifier.py
├── test_chart.py
├── test_interactions.py
├── test_discord_commands.py
└── sample_payloads.json    # Example TradingView alert payloads

scripts/
└── register_commands.py    # One-time: register Discord slash commands via API

investors.json              # Seed file — copied to /data on first startup
trade_record.json           # Seed file — Alpaca win/loss, copied to /data on first startup
pending_orders.json         # Queued orders (written at runtime)
upload_pickle.py            # One-time helper: upload local Robinhood session to Render
test_trade.py               # Manual webhook test script (local development)
Dockerfile
docker-compose.yml
```

---

## Environment Variables

Copy `.env.example` to `.env` for local development. All variables are set in the Render Environment tab for production.

### Required

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `WEBHOOK_SECRET` | Shared secret — must match the `"secret"` field in every TradingView alert payload |

### Alpaca

| Variable | Default | Description |
|---|---|---|
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets/v2` | Switch to `https://api.alpaca.markets` for live trading |
| `ALLOW_FRACTIONAL_SHARES` | `false` | Enable fractional share orders on Alpaca |

### Robinhood

| Variable | Default | Description |
|---|---|---|
| `RH_ENABLED` | `true` | Kill switch — set `false` to disable Robinhood without removing other config |
| `RH_USERNAME` | — | Robinhood account email |
| `RH_PASSWORD` | — | Robinhood account password |
| `RH_LEVERAGE_FACTOR` | `0.3` | Fraction of RH buying power per BUY / ADD_LEVERAGE trade (e.g. `0.9` = 90%) |
| `RH_ACCOUNT_NUMBER` | — | Specific RH account number — leave blank to use the primary account |
| `RH_DISCORD_WEBHOOK_URL` | — | Discord channel for RH trade alerts. Falls back to `DISCORD_WEBHOOK_URL` |
| `RH_SESSION_WEBHOOK_URL` | — | Discord channel for RH session status (expiry, keep-alive, auth). Falls back to `DISCORD_WEBHOOK_URL` |
| `RH_PNL_WEBHOOK_URL` | — | Discord channel for RH P&L reports. Falls back to `DISCORD_WEBHOOK_URL` |

### Discord

| Variable | Description |
|---|---|
| `DISCORD_WEBHOOK_URL` | Main channel — P&L reports, errors, fallback for all other channels |
| `DISCORD_INVESTORS_WEBHOOK_URL` | Investor equity breakdown reports. Falls back to main |
| `DISCORD_TRADES_WEBHOOK_URL` | Alpaca trade alerts (fill price, P&L, win/loss record) |

### Discord Slash Commands

| Variable | Description |
|---|---|
| `DISCORD_APP_PUBLIC_KEY` | Discord app public key — Developer Portal → your app → General Information |
| `DISCORD_APP_ID` | Discord application ID — same page |
| `DISCORD_YOUR_USER_ID` | Your personal Discord user ID — only this user can run slash commands |

### Tax Reports

| Variable | Description |
|---|---|
| `ALPACA_TAX_WEBHOOK_URL` | Discord channel for quarterly Alpaca tax summaries. Falls back to main |
| `RH_TAX_WEBHOOK_URL` | Discord channel for quarterly RH tax summaries. Falls back to main |

### Notifications (optional)

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token — enables Telegram alerts (both values required) |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |

### Persistent Disk

| Variable | Default | Production value |
|---|---|---|
| `INVESTORS_PATH` | `investors.json` | `/data/investors.json` |
| `PENDING_ORDERS_PATH` | `pending_orders.json` | `/data/pending_orders.json` |
| `TRADE_RECORD_PATH` | `trade_record.json` | `/data/trade_record.json` |
| `IDEMPOTENCY_PATH` | `idempotency.json` | `/data/idempotency.json` |

### Other

| Variable | Default | Description |
|---|---|---|
| `IDEMPOTENCY_TTL` | `300` | Seconds to remember a processed alert ID before expiry |
| `LOG_LEVEL` | `INFO` | `DEBUG` for local dev, `INFO` for production |
| `PORT` | `8000` | Server port |

---

## Endpoints

### `GET /health`
Lightweight liveness probe. Returns 200 while the server is running.

```json
{ "status": "ok", "uptime_s": 3600.1, "paper": true }
```

### `GET /healthz`
Deep health check — verifies Alpaca API connectivity and Robinhood session state. Always returns HTTP 200; check the `status` field.

```json
{
  "status": "healthy",
  "alpaca": "up",
  "robinhood": "active",
  "rh_enabled": true,
  "uptime_s": 3600.1,
  "timestamp": "2026-06-08T19:00:00+00:00"
}
```

---

### `POST /webhook`
Main TradingView alert receiver. Validates secret, deduplicates, executes on Alpaca + Robinhood, fires trade notifications.

**Payload — paste this into every TradingView `strategy.alert()` message:**
```json
{
  "secret":                    "YOUR_WEBHOOK_SECRET",
  "ticker":                    "{{ticker}}",
  "action":                    "{{strategy.order.action}}",
  "contracts":                 "{{strategy.order.contracts}}",
  "price":                     "{{close}}",
  "leverage_factor":           0.5,
  "order_id":                  "{{strategy.order.id}}",
  "market_position":           "{{strategy.market_position}}",
  "market_position_size":      "{{strategy.market_position_size}}",
  "prev_market_position":      "{{strategy.prev_market_position}}",
  "prev_market_position_size": "{{strategy.prev_market_position_size}}",
  "timestamp":                 "{{timenow}}"
}
```

`leverage_factor` — fraction of buying power for `add_leverage`/`remove_leverage`. Comes from your TradingView script's input variable. `contracts` is used for legacy `buy`/`sell` actions; Kimi actions calculate qty live from Alpaca buying power.

**Response:**
```json
{
  "status": "ok",
  "result": {
    "action": "add_leverage",
    "ticker": "SPY",
    "orders": [{ "alpaca_order_id": "...", "symbol": "SPY", "side": "buy", "qty": "6.0", "status": "filled" }],
    "robinhood": { "status": "ok", "side": "buy", "qty": 0.1216, "fill_price": 540.13, "position_qty": 0.1216 }
  }
}
```

---

### `POST /deposit`
Record a cash deposit for an investor. Fetches current SPY price from Alpaca if `spy_price` is omitted.

```json
{
  "secret":    "YOUR_WEBHOOK_SECRET",
  "investor":  "Moses",
  "amount":    500,
  "spy_price": null
}
```

`spy_price` — optional. Useful for backdating a deposit to a specific entry price.

---

### `POST /run-report`
Manually trigger a P&L report to Discord.

```json
{ "secret": "YOUR_WEBHOOK_SECRET", "report": "daily" }
```

`report` accepts: `daily`, `weekly`, `monthly`, `ytd`, `1year`, `alltime`, `both` (daily + weekly), `investors`.

---

### `POST /interactions`
Receives Discord slash command interactions. Verifies Ed25519 signature, checks user ID, dispatches commands.

| Command | Parameters | What it does |
|---|---|---|
| `/deposit` | `investor`, `amount`, `spy_price` (optional) | Records a cash deposit. Fetches live SPY price if `spy_price` omitted |
| `/withdraw` | `investor`, `amount` | Records a cash withdrawal. Validates amount ≤ total deposited |
| `/report` | `type` (daily/weekly/monthly/ytd/1year/alltime/both/investors) | Fires a P&L report. Weekly/monthly/yearly/all-time include an equity chart |

All responses are ephemeral (visible only to you). Requires `DISCORD_APP_PUBLIC_KEY`, `DISCORD_APP_ID`, and `DISCORD_YOUR_USER_ID`.

---

### `POST /robinhood-auth`
Re-authenticate Robinhood via SMS 2FA code. Use after a "session expired" Discord alert.

```json
{ "secret": "YOUR_WEBHOOK_SECRET", "sms_code": "123456" }
```

---

### `POST /robinhood-upload-pickle`
Upload a locally-generated Robinhood session pickle to Render. Use for first-time setup or after a hard session expiry. Validates file size (≤ 512 KB) and pickle magic bytes before writing to disk.

```json
{ "secret": "YOUR_WEBHOOK_SECRET", "pickle_b64": "<base64-encoded pickle>" }
```

---

## Trade Actions

| Action | Alpaca behaviour | Robinhood behaviour |
|---|---|---|
| `buy` | Market BUY `contracts` shares | Market BUY (`RH_LEVERAGE_FACTOR` × buying power) |
| `sell` | Market SELL `contracts` shares | Market SELL full position |
| `close_long` | Close entire long position | Sell full position |
| `close_short` | Buy to cover entire short | No-op (shorting not supported on standard RH) |
| `reverse_to_long` | Close short → BUY `contracts` shares | Close any position → BUY |
| `reverse_to_short` | Close long → SELL `contracts` shares | Close long position only |
| `base_entry` | **Ignored** — place manually on Alpaca | Skipped |
| `add_leverage` | BUY `(buying_power × leverage_factor) / price` shares | BUY `(buying_power × RH_LEVERAGE_FACTOR) / price` fractional shares |
| `remove_leverage` | SELL the DCA portion: `total_qty × (lf / (1 + lf))` | Sell full position |
| `stop_loss` | Close all open positions | Sell full position |

**Kimi plain-English alert mapping** — the Pine Script `alert()` call can send plain English strings, which are normalised automatically:

| TradingView string | Maps to |
|---|---|
| `"base entry"` | `base_entry` |
| `"add leverage"` | `add_leverage` |
| `"remove leverage"` | `remove_leverage` |
| `"stop loss"` | `stop_loss` |

---

## Discord Notifications

### Trades channel (`DISCORD_TRADES_WEBHOOK_URL`)
Fires after every Alpaca trade.

**Immediate fill:**
```
🟢 ADD_LEVERAGE — SPY
Qty: 6 shares @ $537.42
Position: 11 shares
🕐 1:32 PM CDT — May 11, 2026
```

**Sell with P&L and win/loss record:**
```
🔴 REMOVE_LEVERAGE — SPY
Qty: 3 shares @ $551.80
Position: 5 shares
P&L: +$43.14 (+2.68%) 🟢 WIN
🕐 2:45 PM CDT — May 11, 2026

Record: 3-1 (75% Win Rate)
```

**After-hours — queued, then filled at open:**
```
⏳ ADD_LEVERAGE — SPY
Order queued for next market open @ ≈$537.42
🕐 3:01 PM CDT — May 11, 2026
```
At 9:31 AM ET next trading day, the system polls Alpaca for the fill and posts:
```
🟢 ADD_LEVERAGE (FILLED AT OPEN) — SPY
Qty: 6 shares @ $538.10
Position: 11 shares
🕐 8:31 AM CDT — May 12, 2026
```

---

### Robinhood channel (`RH_DISCORD_WEBHOOK_URL`)
Mirrors the trades channel for Robinhood. Uses fractional shares and a separate win/loss record.

```
🟢 RH ADD_LEVERAGE — SPY
Qty: 0.1216 shares @ $537.42
Position: 0.1216 shares
🕐 1:32 PM CDT — May 11, 2026
```

RH orders placed after market close are correctly classified as queued (vs. immediate fills) by checking Alpaca's market clock — Robinhood's API always returns `state="unconfirmed"` on submission regardless of market hours, so the API response alone cannot make this distinction.

**Session alerts** (posted to `RH_SESSION_WEBHOOK_URL`):
```
⚠️ ROBINHOOD SESSION EXPIRED — POST /robinhood-auth to re-authenticate.
🔄 ROBINHOOD SESSION REFRESHED — Auto keep-alive successful.
```

---

### Main channel (`DISCORD_WEBHOOK_URL`)
Errors and scheduled P&L reports.

**Daily report (4:00 PM ET Mon–Fri):**
```
📈🟢 Daily P&L — Monday, May 11, 2026
Portfolio: $5,590.32
P&L: +$47.82 (+0.86%)
S&P 500: +0.43% (+0.43% ahead)
```

**Weekly report (4:00 PM ET Fridays) — with equity chart:**
```
📈🟢 Weekly P&L — Week of May 9–16, 2026
Portfolio: $5,590.32
P&L: +$290.32 (+5.47%)
S&P 500: +1.20% (+4.27% ahead)
[equity chart PNG attached]
```

Monthly and yearly reports fire automatically on the last trading day of the period. All types are also available on demand via `/report` or `POST /run-report`.

---

### Investors channel (`DISCORD_INVESTORS_WEBHOOK_URL`)
Daily investor equity breakdown, alongside every daily/weekly report.

```
📊 Investor Breakdown — May 11, 2026
SPY: $537.42

Moses
  Deposited: $300.00
  Current Equity: $312.92
  P&L: +$12.92 (+4.31%)
  Portfolio Share: 5.7%

─────────────────────
Total Portfolio: $5,528.24
Total Deposited: $5,300.00
Overall P&L: +$228.24 (+4.31%)
```

---

## Investor Tracking

`investors.json` stores each investor's deposit history. On Render, it lives at `/data/investors.json` (persistent disk). The repo-root file is a seed — copied to disk on first startup.

```json
{
  "investors": [
    {
      "name": "Moses",
      "deposits": [
        { "amount": 300, "entry_spy": 707.116, "date": "2026-05-09" },
        { "amount": 200, "entry_spy": 720.00,  "date": "2026-05-15" }
      ]
    }
  ]
}
```

Each deposit carries its own SPY entry price so multiple deposits at different times are each tracked independently:

```
equity = deposit.amount × (current_SPY / deposit.entry_spy)
```

**Adding a deposit:** `/deposit` in Discord, or `POST /deposit`. Saves to disk immediately.

**Withdrawals:** `/withdraw` in Discord — appends a negative deposit entry at current SPY price.

---

## Pending Order Handling

When an Alpaca order is placed after market hours, the system:

1. Posts an `⏳ queued for next market open` notification immediately.
2. Saves the order to `pending_orders.json` (persistent disk) so it survives restarts.
3. Schedules a job at 9:31 AM ET on the next trading day to poll Alpaca for the fill.
4. On fill, posts a full `(FILLED AT OPEN)` notification with actual fill price and P&L.
5. Removes the order from `pending_orders.json`.

On startup, `reschedule_pending_orders()` restores any jobs lost during a restart or redeploy.

---

## Win/Loss Record

Two separate records:

- **Alpaca** — `TRADE_RECORD_PATH` (`/data/trade_record.json`). Updated on every Alpaca sell.
- **Robinhood** — `/data/rh_trade_record.json`. Updated on every RH sell.

Both display running win rate: `3-1 (75% Win Rate)`.

For `remove_leverage`, P&L is calculated against the stored ADD_LEVERAGE fill price (from `leverage_entry.json`) rather than the blended position average. This gives an accurate picture of the leverage trade's performance independent of the base position's cost basis.

---

## P&L Reports

### Alpaca (`pnl.py`)

| Type | When | Includes |
|---|---|---|
| Daily | 4:00 PM ET Mon–Fri | Portfolio value, day P&L, SPY comparison |
| Weekly | 4:00 PM ET Fridays | Above + equity chart |
| Monthly | 4:00 PM ET, last trading day of month | Above + equity chart |
| Yearly | 4:00 PM ET, last trading day of year | Above + equity chart |
| YTD / All-time / 1-Year | On demand only | Above + equity chart |

Uses Alpaca's portfolio history API and `yfinance` for SPY data.

### Robinhood (`rh_pnl.py`)

Separate daily, weekly, monthly, and yearly reports posted to `RH_PNL_WEBHOOK_URL`. Tracks RH-specific P&L using RH position history.

---

## Quarterly Tax Reports

On **January 1, April 1, July 1, and October 1** at 8:00 AM ET, the system posts tax summaries for both Alpaca and Robinhood:
- **January 1** → reports the completed prior year.
- **April / July / October 1** → reports the current year to date.

Posted to `ALPACA_TAX_WEBHOOK_URL` and `RH_TAX_WEBHOOK_URL` respectively (both fall back to `DISCORD_WEBHOOK_URL`).

---

## Idempotency

TradingView can fire the same alert multiple times (network retries, bar replays). The system deduplicates using a disk-backed JSON store at `IDEMPOTENCY_PATH`.

A fingerprint is derived from the alert's `order_id` + `ticker` (preferred), or `ticker` + `action` + `timestamp`, or `ticker` + `action` + `contracts` + `price` as a last resort. Fingerprints expire after `IDEMPOTENCY_TTL` seconds (default: 300 / 5 minutes). The file survives Render restarts.

---

## Robinhood Session Setup

Robinhood requires SMS 2FA. The session token is stored at `/data/robinhood.pickle` and auto-refreshed every 72 hours.

### First-time setup

**Step 1 — Generate the pickle locally:**
```powershell
py -c "import robin_stocks.robinhood as r; r.login('YOUR_EMAIL', 'YOUR_PASSWORD', store_session=True)"
```
Enter the SMS code when prompted. This writes `~/.tokens/robinhood.pickle`.

**Step 2 — Upload to Render:**
```powershell
py upload_pickle.py
```
Enter your webhook secret when prompted. You'll see `{"status":"authenticated"}` and a Discord confirmation.

### Session expiry

If the session expires, you'll receive a Discord alert. Re-run the two steps above. Alternatively, if Robinhood sends you an SMS:

```bash
curl -X POST https://your-render-url/robinhood-auth \
     -H "Content-Type: application/json" \
     -d '{"secret":"YOUR_WEBHOOK_SECRET","sms_code":"123456"}'
```

---

## Running Locally

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in ALPACA_API_KEY, ALPACA_SECRET_KEY, WEBHOOK_SECRET in .env
uvicorn app.main:app --reload
```

Or with Docker:
```bash
docker-compose up
```

### Tests
```bash
pytest tests/ -v
```
No real Alpaca, Robinhood, or Discord calls are made — all external clients are mocked.

### Manual webhook test
```bash
py test_trade.py
```
Fires a sample `add_leverage` alert against your local server.

---

## Deployment on Render

1. Push to GitHub.
2. Render dashboard → **New → Web Service** → connect repo.
3. Configure:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add all environment variables in the **Environment** tab.
5. Add a **Persistent Disk** (Disks tab):
   - Mount path: `/data`
   - Size: 1 GB
   - Set `INVESTORS_PATH=/data/investors.json`, `PENDING_ORDERS_PATH=/data/pending_orders.json`, `TRADE_RECORD_PATH=/data/trade_record.json`, `IDEMPOTENCY_PATH=/data/idempotency.json`
   - The Robinhood pickle (`/data/robinhood.pickle`) is written by `upload_pickle.py` — no env var needed.
6. Deploy. Render provides a public HTTPS URL automatically.

---

## Discord Slash Commands — One-Time Setup

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**.
2. Copy **Application ID** and **Public Key** from General Information → add to Render env vars.
3. Enable Developer Mode in Discord (Settings → Advanced) → right-click your username → **Copy User ID** → add as `DISCORD_YOUR_USER_ID`.
4. Set **Interactions Endpoint URL** in Developer Portal → `https://<your-render-url>/interactions`.
5. Add the bot to your server via OAuth2 → URL Generator → scope `applications.commands`.
6. Register slash commands (run once; re-run after changing command definitions):
```bash
DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... py scripts/register_commands.py
```

---

## Switching to Live Trading

1. Create a Live account at [Alpaca](https://alpaca.markets) (requires identity verification).
2. Generate Live API keys.
3. Update in Render Environment:
   ```
   ALPACA_API_KEY=your_live_key
   ALPACA_SECRET_KEY=your_live_secret
   ALPACA_BASE_URL=https://api.alpaca.markets
   ```

Test thoroughly on paper trading before switching. Live orders execute immediately.

---

## Security

- Webhook secret validated with `hmac.compare_digest` (constant-time) to prevent timing-oracle attacks.
- Discord slash commands verified with Ed25519 signature before any processing.
- Robinhood pickle upload validated for file size (≤ 512 KB) and Python pickle magic bytes.
- Slash commands restricted to a single configured user ID (`DISCORD_YOUR_USER_ID`).
- Swagger UI (`/docs`, `/redoc`) is disabled in production.
- Never commit `.env` — it is gitignored. Generate a strong secret:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
