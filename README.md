# Kimi Auto Trade

A production-ready Python / FastAPI service that runs **three parallel trading strategies** on Alpaca and Robinhood simultaneously. Tracks investors, posts trade signals and P&L reports to Discord, and handles queued/after-hours orders automatically.

Deployed on [Render](https://render.com) with a persistent disk at `/data/`.

**Public landing page:** [Kimi Invest](https://moses-log.github.io/kimi-invest-site/) — live SPY performance stats, cumulative return chart, and Whop signup. Powered by `GET /public-stats` on this server.

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
║  │  • stop_loss         │  │                     │  │  Prompt includes:    │   ║
║  │                      │  │  5% buying power    │  │  • Fundamentals      │   ║
║  │  Alpaca (primary)    │  │  per trade on RH    │  │  • Macro (VIX/10Y/   │   ║
║  │  RH (mirror)         │  │                     │  │    CPI)              │   ║
║  │                      │  │  Tracked in         │  │  • Earnings dates    │   ║
║  │  SPY ONLY — never    │  │  claude_portfolio   │  │  • Rebalance history │   ║
║  │  touched by Claude   │  │  .json              │  │                      │   ║
║  └──────────┬───────────┘  └─────────┬───────────┘  └──────────┬───────────┘   ║
║             │                        │    yfinance ─────────────┤               ║
║             │                        │    FRED API  ────────────┤               ║
║             │                        │    (macro + earnings)    │               ║
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
  PUBLIC WEBSITE  (GitHub Pages → Moses-log/kimi-invest-site)
════════════════════════════════════════════════════════════════

  Kimi Invest ──► GET /public-stats            ← SPY LIFO stats + spot counter
  kimiinvest.com   GET /public-claude-callouts ← Claude Manager trade callouts
                   (both: 1-hour in-memory cache, no auth, CORS open)

  Whop.com    ──► POST /whop-webhook           ← membership.went_valid  → decrement spots
                                                  membership.went_invalid → increment spots
                                                  writes /data/early_access.json (0–15)

════════════════════════════════════════════════════════════════
  SCHEDULED JOBS  (APScheduler inside the FastAPI process)
════════════════════════════════════════════════════════════════

  Mon–Thu 4:00 PM ET ──► Alpaca daily P&L  +  RH daily P&L  +  Investor breakdown
  Fri     4:00 PM ET ──► Above  +  weekly P&L charts  +  Portfolio Pie Chart
  1st of month 9:35 AM ET ──► Claude autonomous portfolio rebalance
  Every 72 hours ──────► RH session keep-alive (silent token refresh)
  Jan/Apr/Jul/Oct 1 ───► Quarterly Alpaca + RH tax summaries
  9:31 AM ET next open ► Queued Claude sell fill confirmation (when after-hours)

════════════════════════════════════════════════════════════════
  DISCORD CHANNELS
════════════════════════════════════════════════════════════════

  DISCORD_TRADES_WEBHOOK_URL        ← Alpaca trade fills (Kimi)
  RH_DISCORD_WEBHOOK_URL            ← Robinhood trade fills (Kimi mirror)
  CLAUDE_PORTFOLIO_WEBHOOK_URL      ← Claude Autopilot manual signal trades + fill confirmations
  CLAUDE_MANAGER_WEBHOOK_URL        ← Claude autonomous analysis, rebalance trades, benchmark
  PORTFOLIO_SNAPSHOT_WEBHOOK_URL    ← Friday RH pie chart + valuation rating
  RH_PNL_WEBHOOK_URL                ← Robinhood P&L reports (daily/weekly/monthly)
  RH_SESSION_WEBHOOK_URL            ← RH session alerts (expiry, keep-alive)
  DISCORD_WEBHOOK_URL               ← Main: Alpaca P&L, errors, fallback
  DISCORD_INVESTORS_WEBHOOK_URL     ← Investor equity breakdown
  ALPACA_TAX_WEBHOOK_URL            ← Quarterly Alpaca tax summaries
  RH_TAX_WEBHOOK_URL                ← Quarterly RH tax summaries
  SIGNAL_SUBSCRIBERS_WEBHOOK_URL    ← Paid Kimi Invest Discord: Kimi BUY/SELL signals + WIN/LOSS
  CLAUDE_SUBSCRIBERS_WEBHOOK_URL    ← Paid Kimi Invest Discord: Claude BUY/SELL/TRIM/DOUBLE_DOWN signals

════════════════════════════════════════════════════════════════
  PERSISTENT DISK  (/data on Render)
════════════════════════════════════════════════════════════════

  robinhood.pickle          ← RH session token (auto-refreshed every 72 h)
  investors.json            ← Investor deposit history
  trade_record.json         ← Alpaca win/loss record
  rh_trade_record.json      ← RH win/loss record (Kimi + all Claude sells)
  leverage_entry.json       ← ADD_LEVERAGE fill prices (accurate DCA P&L)
  pending_orders.json       ← Queued orders (Alpaca + Claude sells) — survive restarts
  idempotency.json          ← Seen alert IDs (duplicate suppression, 5 min TTL)
  claude_portfolio.json     ← Claude Autopilot + Manager positions + W-L record
  claude_rebalance_log.json ← Monthly rebalance audit log (36 entries max)
  rh_positions_cache.json   ← Last-known RH positions (pie chart fallback)
  early_access.json         ← Kimi Invest early-access spot counter (0–15, Whop-driven)
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

Positions tracked in `/data/claude_portfolio.json` with entry price, qty, W-L record, and tweet URL. When a sell is placed after market hours, the bot queues a fill confirmation job at 9:31 AM ET the next trading day to resolve the actual fill price and P&L.

---

### 3 — Claude Portfolio Manager (Autonomous)

On the **1st of each month at 9:35 AM ET**, the system:

1. Fetches all RH positions + buying power
2. Enriches each holding with yfinance fundamentals (Forward P/E, ROE, margins, growth, earnings date) — **all tickers in parallel**
3. Fetches **macro context** in parallel: VIX, 10Y Treasury yield, and CPI YoY (via FRED API if configured)
4. Loads **last 3 rebalance log entries** to give Claude its own performance history
5. Calls **claude-opus-4-8** via the Anthropic API with a full Ackman-style scoring framework
6. Claude scores every holding 0–100 across Quality / Growth / Momentum / Valuation / Competitive Advantage — using macro conditions and upcoming earnings dates to inform timing
7. Claude proposes an optimal portfolio using five trade actions:
   - **BUY** — open or add to a position (delta-buy: only invests the additional dollars needed to reach target weight)
   - **DOUBLE_DOWN** — explicitly add to an existing position with elevated conviction (same delta-buy execution, distinct Discord signal)
   - **SELL** — close an entire position
   - **TRIM** — reduce a position to a lower target weight without closing it (blocked if qty < 1 share — Robinhood cannot partially sell fractional positions)
   - **HOLD** — no trade, maintain target weight
8. Sells execute first; buy budget = cash + expected sell proceeds (handles after-hours queued-sell lag)
9. After-hours sells are saved as pending orders and resolved at market open with actual fill price and P&L
10. Full analysis + each trade posted to Discord; full audit log saved to `/data/claude_rebalance_log.json`
11. **Benchmark comparison** posted at completion (and on no-change months): portfolio % vs SPY % month-over-month and since inception

Can also be triggered on-demand via `/rebalance` Discord slash command or `POST /run-rebalance`.

#### Claude Manager Investment Strategy

The strategy is defined entirely in the system prompt at `app/claude_manager.py:37` (`_SYSTEM_PROMPT`). Here is what it instructs Claude to do:

**Objective:** Maximize long-term risk-adjusted returns and outperform the S&P 500 over rolling 3, 5, and 10-year periods.

**Portfolio Constraints:**
- 5–10 stocks maximum. No ETFs, options, leverage, or short positions.
- Only U.S. publicly traded stocks with a market cap above $5 billion.
- Position sizes between 5% and 25% per stock.
- Cash is a valid position — never force deployment into mediocre opportunities.
- SPY is permanently excluded — it is managed by the Kimi DCA strategy and Claude must never touch it.

**Scoring Framework — every stock is scored 0–100:**

| Dimension | Weight | What it measures |
|---|---|---|
| Quality | 30% | ROIC, ROE, gross/operating margin trends, debt-to-equity, interest coverage, FCF consistency |
| Growth | 25% | Revenue growth, EPS growth, FCF growth, TAM expansion, market share gains |
| Momentum | 20% | Relative strength vs S&P 500, 6-month and 12-month price performance, 200-day MA position, institutional accumulation |
| Valuation | 15% | Forward P/E, PEG, EV/EBITDA, FCF yield, DCF estimates |
| Competitive Advantage | 10% | Brand strength, network effects, switching costs, proprietary technology, industry leadership |

**Philosophy:**
- Think like Bill Ackman and Leopold Aschenbrenner — concentrate in the highest-conviction positions, avoid mega-caps where possible, maximize returns.
- Prefer founder-led or highly aligned management. Prefer durable competitive advantages.
- Avoid deteriorating fundamentals, excessive debt, speculative meme stocks, and negative FCF unless growth is exceptional.
- Diversify across industries when possible; no single sector above 40% of portfolio.
- If only 5–7 stocks meet the required standard, do not force diversification.

**Macro and Timing Rules (injected live into the prompt):**
- **Earnings avoidance**: Do not initiate or significantly increase a position within 3 days of earnings unless conviction is very high. Days-to-earnings is provided for every holding.
- **Macro calibration**: VIX, 10-year Treasury yield, and CPI YoY are provided. High VIX → favor defensiveness. Rising yields → pressure on growth multiples. Elevated CPI → watch margin compression.

**Self-awareness (last 3 months of history injected into the prompt):**
- Claude sees its own prior analyses, what trades it proposed, and how the portfolio performed vs SPY each month.
- Prevents momentum-chasing its own prior decisions and allows it to course-correct.

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
├── notifications.py      # All Discord notification helpers (12 channel routes)
├── trade_notifier.py     # Alpaca trade notification + queued order scheduling + signal subscriber broadcast
├── rh_trade_notifier.py  # Robinhood (Kimi) trade notification
├── early_access.py       # Kimi Invest early-access spot counter (persisted to /data/early_access.json)
├── public_stats.py       # Live LIFO stat computation + 1-hour in-memory cache for /public-stats
│
├── pnl.py                # Alpaca P&L engine (daily/weekly/monthly/yearly/YTD/all-time/since-inception/custom)
├── rh_pnl.py             # Robinhood P&L engine (daily/weekly/monthly/yearly)
├── chart.py              # Alpaca portfolio vs SPY equity curve chart (cyberpunk style)
├── portfolio_report.py   # RH pie chart + valuation rating (Friday snapshot)
├── macro_context.py      # Macro indicators for Claude prompt (VIX, 10Y yield, CPI via FRED)
│
├── claude_manager.py     # Autonomous monthly portfolio manager (Anthropic API)
├── claude_portfolio.py   # Claude position tracker — open/close/trim/W-L for both Claude systems
│
├── investors.py          # Investor data model, equity math, Discord report formatting
├── trade_record.py       # Alpaca win/loss counter
├── rh_trade_record.py    # Robinhood win/loss counter (Kimi + Claude sells)
├── leverage_state.py     # Stores ADD_LEVERAGE fill price per ticker for P&L accuracy
├── pending_orders.py     # Persist queued orders to disk (Alpaca + Claude sells)
├── tax.py                # Quarterly Alpaca + RH tax summary reports
├── interactions.py       # Discord Ed25519 signature verification + option parsing
├── discord_commands.py   # All slash command handlers
├── scheduler.py          # APScheduler job registration
│
└── trading/
    ├── alpaca_client.py    # Alpaca SDK wrapper: orders, positions, portfolio, market clock
    ├── order_logic.py      # TradingView action → Alpaca + Robinhood execution (Kimi)
    └── robinhood_client.py # Robinhood auth, session, fractional orders, dollar-amount buys, partial sells

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
| `CLAUDE_PORTFOLIO_WEBHOOK_URL` | — | Discord channel for Claude Autopilot trade signals and fill confirmations |

### Claude Portfolio Manager (Autonomous)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key — required for autonomous monthly rebalance |
| `CLAUDE_MANAGER_WEBHOOK_URL` | Discord channel for Claude analysis, rebalance trades, and benchmark comparisons |
| `FRED_API_KEY` | Free FRED API key (fred.stlouisfed.org → My Account → API Keys) — enables live CPI data in the rebalance prompt. Optional; VIX and 10Y yield work without it. |

### Portfolio Snapshot

| Variable | Description |
|---|---|
| `PORTFOLIO_SNAPSHOT_WEBHOOK_URL` | Discord channel for Friday RH pie chart + valuation ratings |

### Kimi Invest Public Site

| Variable | Description |
|---|---|
| `SIGNAL_SUBSCRIBERS_WEBHOOK_URL` | Paid Kimi Invest Discord — Kimi BUY/SELL signals with WIN/LOSS on sells. No P&L amounts or quantities. |
| `CLAUDE_SUBSCRIBERS_WEBHOOK_URL` | Paid Kimi Invest Discord — Claude Manager BUY/DOUBLE_DOWN/SELL/TRIM signals posted on every autonomous trade. |
| `WHOP_WEBHOOK_SECRET` | HMAC-SHA256 secret for verifying Whop membership webhook payloads. Optional — signature check is skipped if not set. |

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
| `CLAUDE_REBALANCE_LOG_PATH` | `/data/claude_rebalance_log.json` |

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

### `GET /public-stats`
Public, no-auth endpoint powering the Kimi Invest landing page. Pulls all filled SPY orders from Alpaca, runs LIFO matching, and returns live performance stats. Response is cached in memory for 1 hour.

```json
{
  "trades": 8,
  "wins": 5,
  "losses": 3,
  "win_rate": 62.5,
  "profit_factor": 1.84,
  "date_range": { "from": "Apr 22, 2026", "to": "Jun 11, 2026" },
  "cumulative_returns": [{ "trade": 1, "pct": 2.31, "won": true }, ...],
  "spots_remaining": 12
}
```

No sensitive data is exposed — no account equity, dollar P&L, order IDs, or credentials.

---

### `POST /whop-webhook`
Receives Whop membership events and updates the early-access spot counter in `/data/early_access.json`.

- `membership.went_valid` → decrement spots (floor: 0)
- `membership.went_invalid` → increment spots (cap: 15)

Signature verified via `Whop-Signature` HMAC-SHA256 header when `WHOP_WEBHOOK_SECRET` is set.

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

`action` must be `BUY` or `SELL`. Records position in `/data/claude_portfolio.json` and notifies `CLAUDE_PORTFOLIO_WEBHOOK_URL`. After-hours sells are queued and resolved at market open.

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
`report` accepts: `daily`, `weekly`, `monthly`, `ytd`, `1year`, `alltime`, `inception`, `both`, `investors`, or `custom` (requires `"date": "YYYY-MM-DD"` in the body).

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
| `/report alpaca` | `type` | Fires an Alpaca P&L report (daily/weekly/monthly/ytd/1year/alltime/inception/both/investors) |
| `/report custom` | `date` | Fires an Alpaca P&L report since the given date (YYYY-MM-DD) |
| `/report robinhood` | `type` | Fires a Robinhood P&L report (daily/weekly/monthly/ytd/1year/alltime) |
| `/status` | — | Shows live Alpaca + Robinhood account status and all open positions |
| `/positions` | `broker` (opt.) | Lists all open positions with real-time P&L |
| `/close` | `ticker`, `broker` (opt.) | Closes a position by ticker on Alpaca, Robinhood, or both |
| `/rebalance` | — | Triggers the Claude autonomous portfolio rebalance immediately |
| `/portfolio` | — | Posts the RH portfolio pie chart + valuation ratings on-demand |
| `/tax alpaca` | `year` (opt.) | Posts Alpaca realized gains/losses for the given tax year |
| `/tax robinhood` | `year` (opt.) | Posts RH realized gains/losses for the given tax year (Kimi + Claude) |

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

## Trade Actions (Claude Manager)

| Action | JSON field | Behaviour |
|---|---|---|
| `BUY` | `target_weight_pct` | Delta-buy — only invests the additional dollars needed to reach the target weight |
| `DOUBLE_DOWN` | `target_weight_pct` | Same as BUY, but signals elevated conviction — distinct Discord emoji (🔥) |
| `SELL` | — | Closes the entire position |
| `TRIM` | `target_weight_pct` | Sells only the shares needed to reduce to the target weight. **Blocked if position qty < 1 share** (Robinhood cannot partially sell fractional positions). |
| `HOLD` | `target_weight_pct` | No trade executed |

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

— next morning if after-hours —

✅ 🤖 CLAUDE SELL FILLED — FICO
Qty: 0.43 shares @ $1,891.50
P&L: +$22.15 (+2.80%) 🟢 WIN
Claude Record: 5W - 1L
🕐 9:31 AM CDT — June 2, 2026
```

### Claude Manager (`CLAUDE_MANAGER_WEBHOOK_URL`)
The rebalance posts several messages in sequence:

```
🤖 CLAUDE PORTFOLIO MANAGER — MONTHLY REBALANCE
Fetching portfolio and running analysis... 🕐 9:35 AM CT — June 1, 2026

📊 CLAUDE MONTHLY PORTFOLIO ANALYSIS
[Full written analysis — chunked across multiple Discord messages if >2000 chars]

⚡ EXECUTING 4 TRADE(S) — 🕐 9:36 AM CT

🔥 CLAUDE DOUBLE_DOWN — META
Qty: 0.834 shares @ $601.00
Target: 22% weight — Invested: $501.23
🕐 9:36 AM CT

✂️ CLAUDE TRIM — NVDA
Sold 2.000 shares @ $127.50 → reduced to 8% target
P&L: +$84.20 🟢
Claude Record: 7W - 2L
🕐 9:36 AM CT

🔴 CLAUDE SELL — NOW
Qty: 5.5 shares @ $210.00
P&L: +$312.00 (+18.42%) 🟢 WIN
Claude Record: 8W - 2L
🕐 9:36 AM CT

🟢 CLAUDE BUY — FICO
Qty: 1.234 shares @ $1,842.00
Target: 15% weight — Invested: $2,271.73
🕐 9:36 AM CT

✅ CLAUDE PORTFOLIO REBALANCE COMPLETE — 🕐 9:37 AM CT — June 1, 2026

🟢 This month:   Portfolio +3.21%  |  SPY +1.84%  |  Alpha +1.37%
🟢 Since 2026-01-01:  Portfolio +18.50%  |  SPY +12.30%  |  Alpha +6.20%

— or when no trades needed —

✅ NO CHANGES THIS MONTH
Claude determined the current portfolio requires no rebalancing.

🟢 This month:   Portfolio +1.10%  |  SPY +0.82%  |  Alpha +0.28%
```

### Paid Signal Subscribers (`SIGNAL_SUBSCRIBERS_WEBHOOK_URL`)
Broadcast to the paid Kimi Invest Discord channel on every Kimi fill. No quantities, prices, or dollar amounts — subscribers see the signal direction and outcome only.

```
🟢 SIGNAL — BUY SPY
🕐 10:32 AM CDT — June 15, 2026

🔴 SIGNAL — SELL SPY
🟢 WIN
🕐 2:14 PM CDT — June 15, 2026
```

---

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
  META   P/E: 22.1x   Analyst upside: +8.3%
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
| `rh_trade_record.json` | Every Kimi RH sell + every Claude sell | Robinhood win/loss + tax record |
| `leverage_entry.json` | Every `add_leverage` | DCA fill price for accurate P&L |
| `pending_orders.json` | After-hours Alpaca + Claude queued sells | Queued order fill notifications — survive restarts |
| `idempotency.json` | Every webhook hit | Duplicate alert suppression (5-min TTL) |
| `claude_portfolio.json` | `/claude-signal`, Claude Manager | Claude positions + W-L record |
| `claude_rebalance_log.json` | Monthly rebalance | Full audit log — macro context, analysis, positions before, trades executed/skipped, SPY price (capped at 36 entries) |
| `rh_positions_cache.json` | Every successful RH fetch | Last-known positions for pie chart fallback when API is down |
| `early_access.json` | `/whop-webhook` (Whop events) | Kimi Invest spot counter — starts at 15, decrements on new Whop member, increments on cancellation |

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
| Since Inception | On demand | P&L since `FUND_INCEPTION_DATE` (Apr 27, 2026) + equity chart |
| Custom Date | On demand (`/report custom date:YYYY-MM-DD`) | P&L since the given date + equity chart |

### Robinhood (`rh_pnl.py`)
Separate daily, weekly, monthly, and yearly reports posted to `RH_PNL_WEBHOOK_URL`. Tracks RH-specific P&L.

---

## Quarterly Tax Reports

On **January 1, April 1, July 1, and October 1** at 8:00 AM ET:
- **January 1** → reports the completed prior year
- **April / July / October 1** → reports the current year to date

The RH tax report captures **all RH sells** — Kimi strategy trades and all Claude trades (both Autopilot and Manager). Posted to `ALPACA_TAX_WEBHOOK_URL` and `RH_TAX_WEBHOOK_URL`.

---

## Investor Tracking

Each investor's deposit history is stored in `investors.json`. Equity is calculated as:

```
equity = deposit.amount × (current_SPY / deposit.entry_spy)
```

Multiple deposits at different SPY prices are each tracked independently.

The investor breakdown report (`/report alpaca type:Investor Breakdown`, daily Mon–Thu, posted to `DISCORD_INVESTORS_WEBHOOK_URL`) includes a cyberpunk-style donut chart showing each investor's equity and share of the total portfolio.

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

Three separate records updated on every sell:
- **Alpaca** — `TRADE_RECORD_PATH` (`/data/trade_record.json`)
- **Robinhood (tax)** — `/data/rh_trade_record.json` — captures Kimi sells, Claude Autopilot sells, and Claude Manager sells (including TRIM). Used by `/tax robinhood`.
- **Claude** — `/data/claude_portfolio.json` — tracks Claude Autopilot + Manager W-L record independently, shown in Claude trade Discord messages.

For `remove_leverage`, P&L is calculated against the stored ADD_LEVERAGE fill price rather than the blended position average — giving an accurate view of the DCA trade independent of the base position.

---

## Idempotency

TradingView can fire the same alert multiple times. The system deduplicates via a disk-backed JSON store. A fingerprint is derived from `order_id + ticker` (preferred) or `ticker + action + timestamp`. Fingerprints expire after `IDEMPOTENCY_TTL` seconds (default: 300). The file survives restarts.

---

## Pending Order Handling

When an order is placed after market hours:

**Alpaca (Kimi):**
1. Posts `⏳ queued for next market open` notification immediately
2. Saves the order to `pending_orders.json` — survives restarts
3. Schedules a job at 9:31 AM ET next trading day to poll for the fill
4. On fill, posts a full `(FILLED AT OPEN)` notification with actual fill price and P&L

**Robinhood (Claude sells):**
Same flow, but the pending entry has `broker="claude_sell"` and resolves via `notify_claude_pending_sell_fill`, which posts to `CLAUDE_PORTFOLIO_WEBHOOK_URL` (Autopilot) or `CLAUDE_MANAGER_WEBHOOK_URL` (Manager) with actual fill price, P&L, and win/loss record.

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
