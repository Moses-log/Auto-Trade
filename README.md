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
      ├── POST /webhook   → validate → deduplicate → execute on Alpaca
      │                                             → if filled:  trade alert to Discord
      │                                             → if queued:  ⏳ queued alert to Discord
      │                                                           save to persistent disk
      ├── POST /deposit   → record investor deposit → auto-commit to GitHub
      ├── POST /run-report → manually fire P&L report
      └── GET  /health    → uptime check

APScheduler (inside Render)
      ├── 8:31 AM CT Mon–Fri   → Resolve queued orders → full trade alert to Discord
      ├── 3:00 PM CT Mon–Fri   → Daily P&L report      → Discord (main channel)
      ├── 3:01 PM CT Friday    → Weekly P&L report     → Discord (main channel)
      └── 3:02 PM CT Mon–Fri   → Investor breakdown    → Discord (investors channel)
                                  (3:03 PM on Fridays to avoid colliding with weekly report)

Persistent Disk (/data)
      └── pending_orders.json  → survives restarts and redeploys
```

---

## Project Structure

```
app/
├── main.py              # FastAPI app — /webhook, /deposit, /run-report, /health
├── config.py            # All settings loaded from environment variables
├── models.py            # Pydantic models for TradingView alerts and deposits
├── security.py          # Shared-secret validation (constant-time)
├── idempotency.py       # Duplicate-alert suppression with TTL store
├── logging_config.py    # Structured JSON logging
├── notifications.py     # Discord notifications (main / investors / trades channels)
├── pnl.py               # Daily & weekly P&L calculation and reporting
├── scheduler.py         # APScheduler job registration
├── investors.py         # Investor data model, equity math, Discord formatting
├── trade_notifier.py    # Trade alert Discord message (fill price, P&L, queued order handling)
├── pending_orders.py    # Persist queued orders to disk, reschedule on startup
├── github_commit.py     # Auto-commit investors.json to GitHub via REST API
└── trading/
    ├── alpaca_client.py # Alpaca API wrapper + retry logic
    └── order_logic.py   # TradingView action → Alpaca order translation

tests/
├── test_webhook.py
├── test_deposit.py
├── test_investors.py
├── test_pnl.py
├── test_trade_notifier.py
├── test_github_commit.py
├── conftest.py
└── sample_payloads.json

investors.json           # Investor deposit records (source of truth)
pending_orders.json      # Queued orders awaiting next market open (written to persistent disk at runtime)
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

### Optional — GitHub Auto-Commit

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | GitHub personal access token (`repo` scope) — required for `/deposit` auto-commit |
| `GITHUB_REPO` | `Moses-log/Auto-Trade` | Target repo for investors.json commits |

### Optional — Persistent Disk

| Variable | Default | Description |
|---|---|---|
| `PENDING_ORDERS_PATH` | `pending_orders.json` | Path to store queued orders — set to `/data/pending_orders.json` when using Render's persistent disk |

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
Records a new investor deposit. Fetches current SPY price from Alpaca if `spy_price` is omitted. Auto-commits `investors.json` to GitHub on success.

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

`report` accepts `"daily"`, `"weekly"`, or `"both"`.

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
- Daily P&L report (3:00 PM CT)
- Weekly P&L report (3:01 PM CT Fridays)

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

`investors.json` at the repo root stores each investor's deposit history:

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

**Adding a deposit:** call `POST /deposit` — the file is auto-committed to GitHub so it survives Render redeploys.

**Withdrawals:** edit `investors.json` manually and push to GitHub.

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
   - Add env var: `PENDING_ORDERS_PATH=/data/pending_orders.json`
6. Deploy — Render provides a public HTTPS URL automatically

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
- GitHub token requires only `repo` scope — nothing broader
