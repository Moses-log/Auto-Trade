# Kimi Auto Trade

A production-ready Python / FastAPI service that runs **three parallel trading strategies** on Alpaca and Robinhood simultaneously. Tracks investors, posts trade signals and P&L reports to Discord, and handles queued/after-hours orders automatically.

Deployed on [Render](https://render.com) with a persistent disk at `/data/`.

---

## System Visual Map

```
╔══════════════════════════════════════════════════════════════════════════════════╗
║                          KIMI AUTO TRADE SYSTEM                                  ║
╚══════════════════════════════════════════════════════════════════════════════════╝

  ┌─────────────────┐   ┌──────────────────┐   ┌───────────────────────────────┐
  │  TradingView    │   │  You (manually   │   │       APScheduler             │
  │  Pine Script    │   │  read signal &   │   │  1st of month, 9:35 AM ET     │
  │  strategy.alert │   │  confirm trade)  │   │  → Claude Monthly Rebalance   │
  └────────┬────────┘   └────────┬─────────┘   └───────────────┬───────────────┘
           │                     │                              │
           │ POST /webhook        │ POST /claude-signal         │ run_monthly_rebalance()
           ▼                     ▼                              ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║                        FastAPI Server  (Render)                                  ║
║                                                                                  ║
║  ┌──────────────────────┐  ┌─────────────────────┐  ┌──────────────────────┐   ║
║  │   KIMI STRATEGY      │  │  CLAUDE AUTOPILOT   │  │   CLAUDE MANAGER     │   ║
║  │                      │  │  PORTFOLIO          │  │   (Autonomous)       │   ║
║  │  SPY DCA overlay     │  │                     │  │                      │   ║
║  │  • base_entry        │  │  Manual picks from  │  │  Calls Anthropic API │   ║
║  │  • add_leverage      │  │  @theaiportfolios   │  │  (claude-opus-4-8)   │   ║
║  │  • remove_leverage   │  │  Twitter signals    │  │                      │   ║
║  │  • stop_loss         │  │                     │  │  Ackman-style stock  │   ║
║  │                      │  │  5% buying power    │  │  scoring framework   │   ║
║  │  Alpaca (primary)    │  │  per trade on RH    │  │  (Quality/Growth/    │   ║
║  │  RH (mirror)         │  │                     │  │  Momentum/Valuation/ │   ║
║  │                      │  │  Tracked in         │  │  Competitive Moat)   │   ║
║  │  SPY ONLY — never    │  │  claude_portfolio   │  │                      │   ║
║  │  touched by Claude   │  │  .json              │  │  Never touches SPY   │   ║
║  └──────────┬───────────┘  └─────────┬───────────┘  └──────────┬───────────┘   ║
║             │                        │    yfinance ─────────────┤               ║
║             │                        │    (fundamentals for     │               ║
║             │                        │    all RH positions)     │               ║
║             └────────────────────────┴──────────────────────────┘               ║
║                                      │                                          ║
║                         ┌────────────▼────────────┐                            ║
║                         │    Robinhood Client      │                            ║
║                         │    (robin_stocks)        │                            ║
║                         │    fractional shares     │                            ║
║                         └────────────┬────────────┘                            ║
╚══════════════════════════════════════╪═════════════════════════════════════════╝
                                       │
              ┌────────────────────────┴────────────────────────┐
              ▼                                                  ▼
   ┌──────────────────────┐                         ┌──────────────────────┐
   │     ALPACA           │                         │    ROBINHOOD         │
   │  Kimi strategy only  │                         │  All 3 strategies    │
   │  (SPY via DCA)       │                         │  • Kimi SPY mirror   │
   │                      │                         │  • Claude Autopilot  │
   │  alpaca-py SDK       │                         │  • Claude Manager    │
   └──────────────────────┘                         └──────────────────────┘

════════════════════════════════════════════════════════════════
  SCHEDULED JOBS  (APScheduler inside the FastAPI process)
════════════════════════════════════════════════════════════════

  Mon–Thu 4:00 PM ET ──► Alpaca daily P&L  +  RH daily P&L  +  Investor breakdown
  Fri     4:00 PM ET ──► Above  +  weekly P&L charts  +  Portfolio Pie Chart
  1st of month 9:35 AM ET ──► Claude autonomous portfolio rebalance
  Every 72 hours ──────► RH session keep-alive (silent token refresh)
  Jan/Apr/Jul/Oct 1 ───► Quarterly Alpaca + RH tax summaries

════════════════════════════════════════════════════════════════
  DISCORD CHANNELS
════════════════════════════════════════════════════════════════

  DISCORD_TRADES_WEBHOOK_URL        ← Alpaca trade fills (Kimi)
  RH_DISCORD_WEBHOOK_URL            ← Robinhood trade fills (Kimi mirror)
  CLAUDE_PORTFOLIO_WEBHOOK_URL      ← Claude Autopilot manual signal trades
  CLAUDE_MANAGER_WEBHOOK_URL        ← Claude autonomous analysis + rebalance trades
  PORTFOLIO_SNAPSHOT_WEBHOOK_URL    ← Friday RH pie chart + valuation rating
  RH_PNL_WEBHOOK_URL                ← Robinhood P&L reports (daily/weekly/monthly)
  RH_SESSION_WEBHOOK_URL            ← RH session alerts (expiry, keep-alive)
  DISCORD_WEBHOOK_URL               ← Main: Alpaca P&L, errors, fallback
  DISCORD_INVESTORS_WEBHOOK_URL     ← Investor equity breakdown
  ALPACA_TAX_WEBHOOK_URL            ← Quarterly Alpaca tax summaries
  RH_TAX_WEBHOOK_URL                ← Quarterly RH tax summaries

════════════════════════════════════════════════════════════════
  PERSISTENT DISK  (/data on Render)
════════════════════════════════════════════════════════════════

  robinhood.pickle          ← RH session token (auto-refreshed every 72 h)
  investors.json            ← Investor deposit history
  trade_record.json         ← Alpaca win/loss record
  rh_trade_record.json      ← Robinhood win/loss record
  leverage_entry.json       ← ADD_LEVERAGE fill prices (accurate DCA P&L)
  pending_orders.json       ← Queued Alpaca orders (survive restarts)
  idempotency.json          ← Seen alert IDs (duplicate suppression, 5 min TTL)
  claude_portfolio.json     ← Claude Autopilot + Manager positions + W-L record
  claude_rebalance_log.json ← Monthly rebalance audit log (36 entries max)
  rh_positions_cache.json   ← Last-known RH positions (pie chart fallback)
```

---

## The Three Strategies

### 1 — Kimi Strategy (SPY DCA)

Dollar-cost-averaging overlay triggered by TradingView Pine Script alerts. Executes on both Alpaca (primary) and Robinhood (mirror).

- **Base Entry** — intentionally ignored by the bot; you place the initial position manually on Alpaca.
- **Add Leverage** — DCA buy sized as `leverage_factor × Alpaca buying power`.
- **Remove Leverage** — closes only the DCA portion, leaving the base position intact.
- **Stop Loss** — closes the entire position on both brokers.

> SPY is managed exclusively by the Kimi strategy. Neither Claude system will ever touch SPY.

---

### 2 — Claude Autopilot Portfolio

Manual endpoint for trading signals from [@theaiportfolios](https://x.com/theaiportfolios) on Twitter/X. You read the tweet, confirm it's a live signal, and fire the endpoint. The bot executes on Robinhood using `CLAUDE_LEVERAGE_FACTOR` (default 5%) of buying power.

```bash
curl -X POST https://your-render-url/claude-signal \
  -H "Content-Type: application/json" \
  -d '{"secret":"...","ticker":"FICO","action":"BUY","tweet_url":"https://x.com/..."}'
```

Positions tracked in `/data/claude_portfolio.json` with entry price, qty, W-L record, and tweet URL. Discord notifications match the Kimi signal format.

---

### 3 — Claude Portfolio Manager (Autonomous)

On the **1st of each month at 9:35 AM ET**, the system:

1. Fetches all RH positions + buying power
2. Enriches each holding with yfinance fundamentals (Forward P/E, ROE, margins, growth, etc.) — **all tickers in parallel**
3. Calls **claude-opus-4-8** via the Anthropic API with a full Ackman-style scoring framework
4. Claude scores every holding 0–100 across Quality / Growth / Momentum / Valuation / Competitive Advantage
5. Claude proposes an optimal portfolio (5–10 stocks, up to 25% per position, flexible cash)
6. Sells execute first; buy budget = cash + expected sell proceeds (handles after-hours queued-sell lag)
7. Buys are delta-based — only the additional dollars needed to reach the target weight are invested (prevents over-buying into existing positions)
8. Full analysis + each trade posted to Discord; full audit log saved to `/data/claude_rebalance_log.json`

Can also be triggered on-demand via `/rebalance` Discord slash command or `POST /run-rebalance`.

---

### 4 — Portfolio Snapshot (Fridays + on-demand)

Every Friday at 4:00 PM ET (and via `/portfolio` slash command), the system:

1. Fetches all RH positions + buying power (with fallback to `/data/rh_positions_cache.json` if API is unavailable)
2. Generates a **cyberpunk-style donut chart** showing each position as a neon wedge + cash
3. Fetches yfinance valuation data for every stock in parallel
4. Rates the portfolio on:
   - **Current Valuation** — weighted average Forward P/E vs S&P 500 benchmark (~21x) → Undervalued / Fair Value / Moderately Rich / Expensive
   - **Potential Upside** — weighted average analyst price target vs current price → Strong / Good / Moderate / Limited
5. Posts chart + ratings to `PORTFOLIO_SNAPSHOT_WEBHOOK_URL`

---

## Project Structure

```
app/
├── main.py               # FastAPI app — all HTTP endpoints, app lifespan
├── config.py             # All settings via pydantic-settings (env vars / .env)
├── models.py             # Pydantic models: AlertPayload, DepositRequest, TradingAction
├── security.py           # Shared-secret webhook validation (constant-time hmac.compare_digest)
├── idempotency.py        # Duplicate-alert suppression — disk-backed TTL store
├── logging_config.py     # Structured JSON logging setup
│
├── notifications.py      # All Discord notification helpers (11 channel routes)
├── trade_notifier.py     # Alpaca trade notification + queued order scheduling
├── rh_trade_notifier.py  # Robinhood (Kimi) trade notification
│
├── pnl.py                # Alpaca P&L engine (daily/weekly/monthly/yearly/YTD/all-time)
├── rh_pnl.py             # Robinhood P&L engine (daily/weekly/monthly/yearly)
├── chart.py              # Alpaca portfolio vs SPY equity curve chart (cyberpunk style)
├── portfolio_report.py   # RH pie chart + valuation rating (Friday snapshot)
│
├── claude_manager.py     # Autonomous monthly portfolio manager (Anthropic API)
├── claude_portfolio.py   # Claude position tracker — open/close/W-L for both Claude systems
│
├── investors.py          # Investor data model, equity math, Discord report formatting
├── trade_record.py       # Alpaca win/loss counter
├── rh_trade_record.py    # Robinhood win/loss counter
├── leverage_state.py     # Stores ADD_LEVERAGE fill price per ticker for P&L accuracy
├── pending_orders.py     # Persist queued Alpaca orders to disk, survive restarts
├── tax.py                # Quarterly Alpaca + RH tax summary reports
├── interactions.py       # Discord Ed25519 signature verification + option parsing
├── discord_commands.py   # All slash command handlers
├── scheduler.py          # APScheduler job registration
│
└── trading/
    ├── alpaca_client.py    # Alpaca SDK wrapper: orders, positions, portfolio, market clock
    ├── order_logic.py      # TradingView action → Alpaca + Robinhood execution (Kimi)
    └── robinhood_client.py # Robinhood auth, session, fractional orders, dollar-amount buys

scripts/
└── register_commands.py    # One-time: register all Discord slash commands via API
```

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `WEBHOOK_SECRET` | Shared secret — must match the `"secret"` field in every alert payload |

### Alpaca

| Variable | Default | Description |
|---|---|---|
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets/v2` | Switch to `https://api.alpaca.markets` for live trading |
| `ALLOW_FRACTIONAL_SHARES` | `false` | Enable fractional share orders on Alpaca |

### Robinhood

| Variable | Default | Description |
|---|---|---|
| `RH_ENABLED` | `true` | Kill switch — set `false` to disable Robinhood entirely |
| `RH_USERNAME` | — | Robinhood account email |
| `RH_PASSWORD` | — | Robinhood account password |
| `RH_LEVERAGE_FACTOR` | `0.3` | Fraction of RH buying power per Kimi BUY / ADD_LEVERAGE trade |
| `RH_ACCOUNT_NUMBER` | — | Specific RH account number — leave blank for primary account |
| `RH_DISCORD_WEBHOOK_URL` | — | Discord channel for Kimi RH trade alerts |
| `RH_SESSION_WEBHOOK_URL` | — | Discord channel for RH session status (expiry, keep-alive) |
| `RH_PNL_WEBHOOK_URL` | — | Discord channel for RH P&L reports |

### Claude Autopilot Portfolio

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_LEVERAGE_FACTOR` | `0.05` | Fraction of RH buying power per `/claude-signal` trade (5%) |
| `CLAUDE_PORTFOLIO_WEBHOOK_URL` | — | Discord channel for Claude Autopilot trade signals |

### Claude Portfolio Manager (Autonomous)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key — required for autonomous monthly rebalance |
| `CLAUDE_MANAGER_WEBHOOK_URL` | Discord channel for Claude analysis + rebalance trade notifications |

### Portfolio Snapshot

| Variable | Description |
|---|---|
| `PORTFOLIO_SNAPSHOT_WEBHOOK_URL` | Discord channel for Friday RH pie chart + valuation ratings |

### Discord Slash Commands

| Variable | Description |
|---|---|
| `DISCORD_APP_PUBLIC_KEY` | Discord app public key — Developer Portal → General Information |
| `DISCORD_APP_ID` | Discord application ID — same page |
| `DISCORD_BOT_TOKEN` | Discord bot token — Developer Portal → Bot → Reset Token |
| `DISCORD_YOUR_USER_ID` | Your personal Discord user ID — only this user can run slash commands |

### Discord Notification Channels

| Variable | Description |
|---|---|
| `DISCORD_WEBHOOK_URL` | Main channel — Alpaca P&L, errors, fallback for all other channels |
| `DISCORD_INVESTORS_WEBHOOK_URL` | Investor equity breakdown reports |
| `DISCORD_TRADES_WEBHOOK_URL` | Alpaca trade alerts (fill price, P&L, win/loss) |
| `ALPACA_TAX_WEBHOOK_URL` | Quarterly Alpaca tax summaries |
| `RH_TAX_WEBHOOK_URL` | Quarterly RH tax summaries |

### Persistent Disk Paths (Render)

| Variable | Production value |
|---|---|
| `INVESTORS_PATH` | `/data/investors.json` |
| `PENDING_ORDERS_PATH` | `/data/pending_orders.json` |
| `TRADE_RECORD_PATH` | `/data/trade_record.json` |
| `IDEMPOTENCY_PATH` | `/data/idempotency.json` |
| `RH_TRADE_RECORD_PATH` | `/data/rh_trade_record.json` |
| `LEVERAGE_STATE_PATH` | `/data/leverage_entry.json` |
| `CLAUDE_PORTFOLIO_PATH` | `/data/claude_portfolio.json` |

### Other

| Variable | Default | Description |
|---|---|---|
| `IDEMPOTENCY_TTL` | `300` | Seconds to remember a processed alert ID |
| `LOG_LEVEL` | `INFO` | `DEBUG` for local dev |
| `PORT` | `8000` | Server port |

---

## Endpoints

### `GET /health`
Lightweight liveness probe.
```json
{ "status": "ok", "uptime_s": 3600.1, "paper": true }
```

### `GET /healthz`
Deep health check — verifies Alpaca API + Robinhood session. Always returns HTTP 200; check `status` field.

---

### `POST /webhook`
Main TradingView alert receiver (Kimi strategy). Validates secret, deduplicates, executes on Alpaca + Robinhood.

**Payload:**
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

---

### `POST /claude-signal`
Manually execute a Claude Autopilot trade from a Twitter signal. Uses `CLAUDE_LEVERAGE_FACTOR` sizing on Robinhood.

```json
{
  "secret":    "YOUR_WEBHOOK_SECRET",
  "ticker":    "FICO",
  "action":    "BUY",
  "tweet_url": "https://x.com/theaiportfolios/status/..."
}
```

`action` must be `BUY` or `SELL`. Records position in `/data/claude_portfolio.json` and notifies `CLAUDE_PORTFOLIO_WEBHOOK_URL`.

---

### `POST /run-rebalance`
Manually trigger the Claude autonomous portfolio rebalance (same as `/rebalance` slash command).

```json
{ "secret": "YOUR_WEBHOOK_SECRET" }
```

Runs in background — watch `CLAUDE_MANAGER_WEBHOOK_URL` Discord channel for updates.

---

### `POST /deposit`
Record a cash deposit for an investor.
```json
{ "secret": "...", "investor": "Moses", "amount": 500, "spy_price": null }
```

### `POST /run-report`
Manually trigger a P&L report.
```json
{ "secret": "...", "report": "daily" }
```
`report` accepts: `daily`, `weekly`, `monthly`, `ytd`, `1year`, `alltime`, `both`, `investors`.

### `POST /robinhood-auth`
Re-authenticate Robinhood via SMS 2FA code.
```json
{ "secret": "...", "sms_code": "123456" }
```

### `POST /robinhood-upload-pickle`
Upload a locally-generated Robinhood session pickle to Render.
```json
{ "secret": "...", "pickle_b64": "<base64>" }
```

---

## Discord Slash Commands

All commands are ephemeral (only visible to you) and restricted to `DISCORD_YOUR_USER_ID`.

| Command | Parameters | What it does |
|---|---|---|
| `/deposit` | `investor`, `amount`, `spy_price` (opt.) | Records a cash deposit at current or specified SPY price |
| `/withdraw` | `investor`, `amount` | Records a cash withdrawal |
| `/report alpaca` | `type` | Fires an Alpaca P&L report (daily/weekly/monthly/ytd/1year/alltime/both/investors) |
| `/report robinhood` | `type` | Fires a Robinhood P&L report (daily/weekly/monthly/ytd/1year/alltime) |
| `/status` | — | Shows live Alpaca + Robinhood account status and all open positions |
| `/positions` | `broker` (opt.) | Lists all open positions with real-time P&L |
| `/close` | `ticker`, `broker` (opt.) | Closes a position by ticker on Alpaca, Robinhood, or both |
| `/rebalance` | — | Triggers the Claude autonomous portfolio rebalance immediately |
| `/portfolio` | — | Posts the RH portfolio pie chart + valuation ratings on-demand |
| `/tax alpaca` | `year` (opt.) | Posts Alpaca realized gains/losses for the given tax year |
| `/tax robinhood` | `year` (opt.) | Posts RH realized gains/losses for the given tax year |

**One-time setup:** After any command changes, re-run:
```powershell
$env:DISCORD_APP_ID="..."; $env:DISCORD_BOT_TOKEN="..."; python scripts/register_commands.py
```

---

## Trade Actions (Kimi Strategy)

| Action | Alpaca behaviour | Robinhood behaviour |
|---|---|---|
| `buy` | Market BUY `contracts` shares | Market BUY (`RH_LEVERAGE_FACTOR` × buying power) |
| `sell` | Market SELL `contracts` shares | Market SELL full position |
| `close_long` | Close entire long position | Sell full position |
| `base_entry` | **Ignored** — place manually | Skipped |
| `add_leverage` | BUY `(buying_power × leverage_factor) / price` shares | BUY `(buying_power × RH_LEVERAGE_FACTOR) / price` fractional shares |
| `remove_leverage` | SELL only the DCA portion | Sell full position |
| `stop_loss` | Close all positions | Sell full position |

**Plain-English alert mapping:**

| TradingView string | Maps to |
|---|---|
| `"base entry"` | `base_entry` |
| `"add leverage"` | `add_leverage` |
| `"remove leverage"` | `remove_leverage` |
| `"stop loss"` | `stop_loss` |

---

## Discord Notifications

### Kimi Alpaca (`DISCORD_TRADES_WEBHOOK_URL`)
```
🟢 ADD_LEVERAGE — SPY
Qty: 6 shares @ $537.42
Position: 11 shares
P&L: +$43.14 (+2.68%) 🟢 WIN
Record: 3-1 (75% Win Rate)
🕐 1:32 PM CDT — May 11, 2026
```

### Kimi Robinhood (`RH_DISCORD_WEBHOOK_URL`)
```
🟢 RH ADD_LEVERAGE — SPY
Qty: 0.1216 shares @ $537.42
Position: 0.1216 shares
🕐 1:32 PM CDT — May 11, 2026
```

### Claude Autopilot (`CLAUDE_PORTFOLIO_WEBHOOK_URL`)
```
🟢 🤖 CLAUDE BUY — FICO
Qty: 0.43 shares @ $1,840.00
🕐 2:15 PM CDT — June 1, 2026
📌 https://x.com/theaiportfolios/status/...
```

### Claude Manager (`CLAUDE_MANAGER_WEBHOOK_URL`)
Long analysis text (chunked if >2000 chars), followed by individual trade signals:
```
⏳ CLAUDE SELL — GEV (queued for open)
Qty: 1 shares ≈ $420.50
🕐 6:12 PM CDT — June 1, 2026

🟢 CLAUDE BUY — FICO
Qty: 0.82 shares @ $1,840.00
Target: 15% weight — Invested: $1,507.00
🕐 9:37 AM CDT — June 1, 2026
```

### Portfolio Snapshot (`PORTFOLIO_SNAPSHOT_WEBHOOK_URL`)
Text + attached PNG donut chart (every Friday + `/portfolio`):
```
📊 PORTFOLIO SNAPSHOT — June 6, 2026
Total Value: $12,450.83

Current Valuation:  🟡 Fair Value (24.3x forward P/E)
Potential Upside:   🟢 Good (+18.5% to analyst targets)
*(S&P 500 benchmark: ~21x forward P/E)*

Per position:
  FICO   P/E: 38.2x   Analyst upside: +12.4%
  GEV    P/E: N/A      Analyst upside: +22.1%
  Cash   P/E: N/A      Analyst upside: N/A

🕐 4:00 PM CDT
```

---

## Persistent State

All data files live on Render's persistent disk at `/data/`. They survive deploys and restarts.

| File | Updated by | Purpose |
|---|---|---|
| `robinhood.pickle` | Auth endpoints, keep-alive | RH session token |
| `investors.json` | `/deposit`, `/withdraw` | Investor deposit history |
| `trade_record.json` | Every Alpaca sell | Alpaca win/loss record |
| `rh_trade_record.json` | Every Kimi RH sell | Robinhood win/loss record |
| `leverage_entry.json` | Every `add_leverage` | DCA fill price for accurate P&L |
| `pending_orders.json` | After-hours Alpaca orders | Queued order fill notifications |
| `idempotency.json` | Every webhook hit | Duplicate alert suppression (5-min TTL) |
| `claude_portfolio.json` | `/claude-signal`, Claude Manager | Claude positions + W-L record |
| `claude_rebalance_log.json` | Monthly rebalance | Full audit log — analysis, positions before, trades executed/skipped (capped at 36 entries) |
| `rh_positions_cache.json` | Every successful RH fetch | Last-known positions for pie chart fallback when API is down |

---

## P&L Reports

### Alpaca (`pnl.py`)

| Type | Trigger | Includes |
|---|---|---|
| Daily | 4:00 PM ET Mon–Fri | Portfolio value, day P&L, SPY comparison |
| Weekly | 4:00 PM ET Fridays | Above + equity chart |
| Monthly | Last trading day of month | Above + equity chart |
| Yearly | Last trading day of year | Above + equity chart |
| YTD / All-time / 1-Year | On demand | Above + equity chart |

### Robinhood (`rh_pnl.py`)
Separate daily, weekly, monthly, and yearly reports posted to `RH_PNL_WEBHOOK_URL`. Tracks RH-specific P&L.

---

## Quarterly Tax Reports

On **January 1, April 1, July 1, and October 1** at 8:00 AM ET:
- **January 1** → reports the completed prior year
- **April / July / October 1** → reports the current year to date

Posted to `ALPACA_TAX_WEBHOOK_URL` and `RH_TAX_WEBHOOK_URL` (both fall back to `DISCORD_WEBHOOK_URL`).

---

## Investor Tracking

Each investor's deposit history is stored in `investors.json`. Equity is calculated as:

```
equity = deposit.amount × (current_SPY / deposit.entry_spy)
```

Multiple deposits at different SPY prices are each tracked independently.

```json
{
  "investors": [
    {
      "name": "Moses",
      "deposits": [
        { "amount": 300, "entry_spy": 707.116, "date": "2026-05-09" }
      ]
    }
  ]
}
```

---

## Win/Loss Record

Two separate records updated on every sell:
- **Alpaca** — `TRADE_RECORD_PATH` (`/data/trade_record.json`)
- **Robinhood** — `/data/rh_trade_record.json`
- **Claude** — `/data/claude_portfolio.json` (shared between Autopilot and Manager)

For `remove_leverage`, P&L is calculated against the stored ADD_LEVERAGE fill price rather than the blended position average — giving an accurate view of the DCA trade independent of the base position.

---

## Idempotency

TradingView can fire the same alert multiple times. The system deduplicates via a disk-backed JSON store. A fingerprint is derived from `order_id + ticker` (preferred) or `ticker + action + timestamp`. Fingerprints expire after `IDEMPOTENCY_TTL` seconds (default: 300). The file survives restarts.

---

## Pending Order Handling

When an Alpaca order is placed after market hours:
1. Posts `⏳ queued for next market open` notification immediately
2. Saves the order to `pending_orders.json` — survives restarts
3. Schedules a job at 9:31 AM ET next trading day to poll for the fill
4. On fill, posts a full `(FILLED AT OPEN)` notification with actual fill price and P&L

---

## Robinhood Session Setup

Robinhood requires SMS 2FA. The session token is stored at `/data/robinhood.pickle` and auto-refreshed every 72 hours.

**First-time setup:**
```powershell
# Step 1 — Generate pickle locally
py -c "import robin_stocks.robinhood as r; r.login('YOUR_EMAIL', 'YOUR_PASSWORD', store_session=True)"

# Step 2 — Upload to Render
py upload_pickle.py
```

**Session expiry** — re-run the two steps above, or:
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
# Fill in ALPACA_API_KEY, ALPACA_SECRET_KEY, WEBHOOK_SECRET
uvicorn app.main:app --reload
```

Or with Docker:
```bash
docker-compose up
```

**Tests:**
```bash
pytest tests/ -v
```
No real Alpaca, Robinhood, or Discord calls are made — all external clients are mocked.

---

## Deployment on Render

1. Push to GitHub — Render auto-deploys on every push to `main`.
2. **New → Web Service** → connect repo.
3. Configure:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add all environment variables in the **Environment** tab.
5. Add a **Persistent Disk** (Disks tab):
   - Mount path: `/data`
   - Size: 1 GB
   - Set all `*_PATH` env vars to `/data/<filename>`

---

## Security

- Webhook secret validated with `hmac.compare_digest` (constant-time) — prevents timing-oracle attacks
- Discord slash commands verified with Ed25519 signature before any processing
- Slash commands restricted to a single configured user ID (`DISCORD_YOUR_USER_ID`)
- Robinhood pickle upload validated for file size (≤ 512 KB) and pickle magic bytes
- Swagger UI (`/docs`, `/redoc`) disabled in production

Generate a strong webhook secret:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
