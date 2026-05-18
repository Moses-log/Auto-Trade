# Auto-Trade — TradingView → Alpaca

A production-ready Python / FastAPI service that receives TradingView strategy alerts, executes trades through Alpaca, tracks a shared investor portfolio, and posts reports to Discord.

Deployed on [Render](https://render.com).

---

## System Overview

```
TradingView alert
      │
      ▼
Render (FastAPI)
      ├── POST /webhook      → validate → deduplicate → execute on Alpaca
      │                                               → if filled:  trade alert to Discord
      │                                               → if queued:  ⏳ queued alert to Discord
      │                                                             save to persistent disk
      ├── POST /interactions → Discord slash commands (/deposit, /withdraw, /report)
      ├── POST /deposit      → record investor deposit → auto-commit to GitHub
      ├── POST /run-report   → manually fire P&L report
      └── GET  /health       → uptime check

APScheduler (inside Render)
      ├── 8:31 AM CT Mon–Fri        → Resolve queued orders  → full trade alert to Discord
      ├── 3:00 PM CT Mon–Fri        → Daily P&L report       → Discord (main channel)
      ├── 3:01 PM CT Friday         → Weekly P&L + chart     → Discord (main channel)
      ├── 3:02 PM CT Mon–Fri        → Investor breakdown     → Discord (investors channel)
      │                                (3:03 PM on Fridays to avoid colliding with weekly report)
      └── 3:05 PM CT Mon–Fri        → Monthly P&L + chart    → Discord (last trading day of month)
                                      Yearly P&L + chart     → Discord (last trading day of Dec)

Persistent Disk (/data)
      ├── investors.json       → deposit records, survives restarts and redeploys
      ├── pending_orders.json  → queued orders awaiting next market open
      ├── trade_record.json    → win/loss record
      └── idempotency.json     → seen alert IDs (TTL-based deduplication)
```

---

## Project Structure

```
app/
├── main.py              # FastAPI app — /webhook, /interactions, /deposit, /run-report, /health
├── config.py            # All settings loaded from environment variables
├── models.py            # Pydantic models for TradingView alerts and deposits
├── security.py          # Shared-secret validation (constant-time)
├── idempotency.py       # Duplicate-alert suppression with disk-backed TTL store
├── logging_config.py    # Structured JSON logging
├── notifications.py     # Discord notifications (main / investors / trades channels)
├── pnl.py               # P&L calculation and reporting (daily/weekly/monthly/yearly/YTD/all-time)
├── chart.py             # Portfolio vs SPY % return equity chart (PNG via matplotlib)
├── scheduler.py         # APScheduler job registration
├── investors.py         # Investor data model, equity math, Discord formatting
├── trade_notifier.py    # Trade alert Discord message (fill price, P&L, queued order handling)
├── pending_orders.py    # Persist queued orders to disk, reschedule on startup
├── interactions.py      # Discord Ed25519 signature verification and routing helpers
├── discord_commands.py  # /deposit, /withdraw, /report slash command handlers
├── trade_record.py      # Win/loss record — disk-backed, updated after every sell
└── trading/
    ├── alpaca_client.py # Alpaca API wrapper + retry logic
    └── order_logic.py   # TradingView action → Alpaca order translation

tests/
├── test_webhook.py
├── test_deposit.py
├── test_investors.py
├── test_pnl.py
├── test_trade_notifier.py
├── test_chart.py
├── conftest.py
└── sample_payloads.json

investors.json           # Seed file — copied to persistent disk on first startup
trade_record.json        # Seed file — copied to persistent disk on first startup
pending_orders.json      # Queued orders (written to persistent disk at runtime)
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in values. All are set in the Render dashboard for production.

### Required

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `WEBHOOK_SECRET` | Shared secret — must match every TradingView alert payload |

### Optional — Alpaca

| Variable | Default | Description |
|---|---|---|
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets/v2` | Switch to `https://api.alpaca.markets` for live trading |
| `ALLOW_FRACTIONAL_SHARES` | `false` | Enable fractional share orders |

### Optional — Discord

| Variable | Description |
|---|---|
| `DISCORD_WEBHOOK_URL` | Main channel — P&L reports and error alerts |
| `DISCORD_INVESTORS_WEBHOOK_URL` | Investor breakdown channel (falls back to main if unset) |
| `DISCORD_TRADES_WEBHOOK_URL` | Trade alert channel — detailed fill info and P&L on sells |

### Optional — Persistent Disk

| Variable | Default | Description |
|---|---|---|
| `INVESTORS_PATH` | `investors.json` | Path to investors file — set to `/data/investors.json` when using Render's persistent disk |
| `PENDING_ORDERS_PATH` | `pending_orders.json` | Path to store queued orders — set to `/data/pending_orders.json` when using Render's persistent disk |
| `TRADE_RECORD_PATH` | `trade_record.json` | Path to store win/loss record — set to `/data/trade_record.json` when using Render's persistent disk |
| `IDEMPOTENCY_PATH` | `idempotency.json` | Path to store seen alert IDs — set to `/data/idempotency.json` when using Render's persistent disk |

### Optional — Discord Slash Commands

| Variable | Description |
|---|---|
| `DISCORD_APP_PUBLIC_KEY` | Discord app public key — from Developer Portal → app → General Information |
| `DISCORD_APP_ID` | Discord application ID — same page |
| `DISCORD_YOUR_USER_ID` | Your personal Discord user ID — only this user can run slash commands |

---

## Endpoints

### `POST /webhook`
Receives TradingView alerts. Validates secret, deduplicates, executes trade on Alpaca, fires trade alert to Discord.

**Payload:**
```json
{
  "secret":                    "YOUR_WEBHOOK_SECRET",
  "ticker":                    "{{ticker}}",
  "action":                    "{{strategy.order.action}}",
  "contracts":                 "{{strategy.order.contracts}}",
  "price":                     "{{close}}",
  "order_id":                  "{{strategy.order.id}}",
  "market_position":           "{{strategy.market_position}}",
  "market_position_size":      "{{strategy.market_position_size}}",
  "prev_market_position":      "{{strategy.prev_market_position}}",
  "prev_market_position_size": "{{strategy.prev_market_position_size}}",
  "timestamp":                 "{{timenow}}"
}
```

### `POST /deposit`
Records a new investor deposit. Fetches current SPY price from Alpaca if `spy_price` is omitted. Saves to persistent disk immediately.

**Payload:**
```json
{
  "secret":    "YOUR_WEBHOOK_SECRET",
  "investor":  "Moses",
  "amount":    500,
  "spy_price": null
}
```

`spy_price` — optional. If `null`, current SPY price is fetched automatically. If provided, uses that value (useful for recording a past deposit).

### `POST /run-report`
Manually fires a P&L report to Discord. Useful if the scheduled report failed.

**Payload:**
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "report": "daily"
}
```

`report` accepts `"daily"`, `"weekly"`, `"monthly"`, `"ytd"`, `"1year"`, `"alltime"`, `"both"`, or `"investors"`.

### `POST /interactions`
Receives Discord slash command interactions. Verifies Ed25519 signature, checks user ID, and dispatches commands as background tasks.

| Command | Parameters | What it does |
|---|---|---|
| `/deposit` | `investor`, `amount`, `spy_price` (optional) | Records a cash deposit. Fetches live SPY price if `spy_price` omitted. |
| `/withdraw` | `investor`, `amount` | Records a cash withdrawal. Validates amount ≤ total deposited. |
| `/report` | `type` (daily/weekly/monthly/ytd/1year/alltime/both/investors) | Fires a P&L report to Discord. Weekly, monthly, yearly, and all-time reports include an equity chart. |

All responses are ephemeral (only visible to you). Requires `DISCORD_APP_PUBLIC_KEY`, `DISCORD_APP_ID`, and `DISCORD_YOUR_USER_ID` env vars.

### `GET /health`
Returns server uptime and paper/live mode status.

---

## Supported Trade Actions

| Action | What happens |
|---|---|
| `buy` | Market BUY for `contracts` shares |
| `sell` | Market SELL for `contracts` shares |
| `close_long` | Close entire long position |
| `close_short` | Close entire short position |
| `reverse_to_long` | Close short → Market BUY `contracts` shares |
| `reverse_to_short` | Close long → Market SELL `contracts` shares |
| `base_entry` | Manual entry — ignored by the system |
| `add_leverage` | Buy using a calculated portion of buying power |
| `remove_leverage` | Close the leverage portion of the position |
| `stop_loss` | Close all open positions |

---

## Discord Channels

### Main channel (`DISCORD_WEBHOOK_URL`)
- ⚠️ / ❌ Trade errors and failures
- Daily P&L report (3:00 PM CT Mon–Fri)
- Weekly P&L report + chart (3:01 PM CT Fridays)
- Monthly P&L report + chart (3:05 PM CT — last trading day of each month)
- Yearly P&L report + chart (3:05 PM CT — last trading day of December)
- YTD, 1-Year, All-Time reports available on demand via `/report` or `POST /run-report`

P&L report format:
```
📈🟢 Weekly P&L — Week of May 9–16, 2026
Portfolio: $5,590.32
P&L: +$290.32 (+5.47%)
S&P 500: +1.20% (+4.27% ahead)
[equity chart image attached]
```

### Investors channel (`DISCORD_INVESTORS_WEBHOOK_URL`)
Daily and weekly investor equity breakdown:
```
📊 Investor Breakdown — May 11, 2026
SPY: $537.42

**Moses**
> Deposited: $300.00
> Current Equity: $312.92
> P&L: +$12.92 (+4.31%)
> Portfolio Share: 5.7%
...
─────────────────────────
**Total Portfolio: $5,528.24**
**Total Deposited: $5,300.00**
**Overall P&L: +$228.24 (+4.31%)**
```

### Trades channel (`DISCORD_TRADES_WEBHOOK_URL`)
Fires after every trade. If an order fills immediately:
```
🟢 ADD_LEVERAGE — SPY
Qty: 7.5 shares @ $537.42
Position: 14.5 shares
🕐 1:32 PM CDT — May 11, 2026
```
On sells, includes P&L and win/loss record:
```
🔴 SELL — SPY
Qty: 7.5 shares @ $551.80
Position: 0 shares
P&L: +$71.90 (+2.68%) 🟢 WIN
🕐 2:45 PM CDT — May 11, 2026

Record: 3-1 (75% Win Rate)
```
If an order is placed after market close, two messages are sent — one immediately, one at next market open when filled:
```
⏳ ADD_LEVERAGE — SPY
Order queued for next market open @ ≈$537.42
🕐 3:00 PM CDT — May 11, 2026
```
```
🟢 ADD_LEVERAGE (FILLED AT OPEN) — SPY
Qty: 7.5 shares @ $538.10
Position: 14.5 shares
🕐 8:31 AM CDT — May 12, 2026
```

---

## Investor Tracking

`investors.json` stores each investor's deposit history on the persistent disk (`/data/investors.json` on Render). The repo root `investors.json` serves as a seed file — copied to disk on first startup.

```json
{
  "investors": [
    {
      "name": "Moses",
      "deposits": [
        {"amount": 300, "entry_spy": 707.116, "date": "2026-05-09"}
      ]
    }
  ]
}
```

Each investor's current equity is computed as:
```
equity = sum( deposit.amount × (current_SPY / deposit.entry_spy) )
```

Every deposit has its own entry price so multiple deposits per investor are handled correctly.

**Adding a deposit:** use `/deposit` in Discord or call `POST /deposit` — saved to persistent disk immediately.

**Withdrawals:** use `/withdraw` in Discord — adds a negative deposit entry at current SPY price.

---

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

No real Alpaca or GitHub calls are made — all external clients are mocked.

---

## Deployment (Render)

1. Push to GitHub
2. Render dashboard → New → Web Service → connect repo
3. Set:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add all environment variables in the Render Environment tab
5. Add a **Persistent Disk** (Disks tab):
   - Mount path: `/data`
   - Size: 1 GB
   - Add env vars: `INVESTORS_PATH=/data/investors.json`, `PENDING_ORDERS_PATH=/data/pending_orders.json`, `TRADE_RECORD_PATH=/data/trade_record.json`, and `IDEMPOTENCY_PATH=/data/idempotency.json`
6. Deploy — Render provides a public HTTPS URL automatically

### Discord Slash Commands Setup (one-time)

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. Copy **Application ID** and **Public Key** from General Information → add to Render env vars
3. Enable Developer Mode in Discord (Settings → Advanced) → right-click your username → **Copy User ID** → add as `DISCORD_YOUR_USER_ID`
4. Set **Interactions Endpoint URL** in Developer Portal → `https://<your-render-url>/interactions`
5. Add the bot to your server via OAuth2 → URL Generator → scope `applications.commands`
6. Register the 3 slash commands (run once locally):
```bash
DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... py scripts/register_commands.py
```

---

## Switching to Live Trading

1. Create a Live account at Alpaca (requires identity verification)
2. Generate Live API keys
3. Update in Render Environment:
   ```
   ALPACA_API_KEY=your_live_key
   ALPACA_SECRET_KEY=your_live_secret
   ALPACA_BASE_URL=https://api.alpaca.markets
   ```

Test thoroughly on paper trading before switching.

---

## Security

- Webhook secret validated with `hmac.compare_digest` to prevent timing attacks
- Never commit `.env` — it is gitignored by default
- Swagger UI (`/docs`) is disabled in production
- Discord slash commands restricted to a single user ID (`DISCORD_YOUR_USER_ID`)
