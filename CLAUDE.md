# Kimi System — Claude Context

## The Kimi System: Naming Map

Every component of the Kimi System has a canonical name. Use these names in conversation
and Claude will know exactly which part you mean without further explanation.

---

### Strategies — the brains that make trading decisions

| Name | What it is |
|---|---|
| **Kimi SPY** | TradingView-triggered automated DCA on SPY. Alpaca is primary execution; RH mirrors every trade. |
| **Kimi Manager** | Autonomous monthly Claude (Opus) rebalance of a long-term equity portfolio. RH only. Runs on the 1st of each month at 9:35 AM ET. |
| **Kimi Autopilot** | Manual signals sourced from @theaiportfolios on X, forwarded to RH for execution. |

---

### Brokers — where trades actually execute

| Name | What it is |
|---|---|
| **Alpaca** | Live brokerage used exclusively by Kimi SPY. Primary execution venue. |
| **RH** | Robinhood. Mirrors Kimi SPY and executes Kimi Autopilot and Kimi Manager. |

---

### Servers — the Discord audiences

| Name | What it is |
|---|---|
| **KI Server** | The paid Kimi Invest Discord. Subscribers (accessed via kimiinvest.com/Whop) see SPY signals, Manager callouts, RH alerts, politician tracker, vetted investor alerts. |
| **Private Server** | Owner-only Discord. Receives all raw execution fills, P&L reports, investor breakdown, session alerts, tax summaries — full detail, no filtering. |

Both servers receive signals from all three strategies but at different detail levels.

---

### The Site — the public face

| Name | What it is |
|---|---|
| **KI Site** | kimiinvest.com — public marketing site and live transparency dashboard. Pages: index, trades, portfolio, stats. Pulls from the public-stats endpoint on Kimi API. |

---

### The Engine — the backend that runs everything

| Name | What it is |
|---|---|
| **Kimi API** | FastAPI server deployed on Render. Handles all webhooks (TradingView), Discord slash commands, HTTP endpoints, and routes signals to brokers. Entry point: `app/main.py`. |
| **Scheduler** | APScheduler cron jobs inside Kimi API. Drives all automatic behaviour: P&L reports, Kimi Manager monthly rebalance, RH keep-alive, nightly backup. Defined in `app/scheduler.py`. |
| **RH Session** | Robinhood authentication manager. Handles initial login (via pickle), keep-alive refreshes every 1–2 days between 1–5 AM ET, and re-auth flow when the session expires. |
| **Gist Backup** | Nightly push of all `/data/` JSON files to a private GitHub Gist. Fires at midnight ET via the Scheduler. |

---

### Reports — automated outputs from the Scheduler

| Name | What it is |
|---|---|
| **Alpaca Reports** | Daily and weekly Alpaca P&L with SPY comparison and chart. Sent to Private Server at 4 PM ET on trading days. Monthly and yearly fire on the last trading day of the period. |
| **RH Reports** | Daily and weekly RH P&L with SPY comparison. Also includes the Friday portfolio pie chart (positions + valuation ratings). Sent to both KI Server and Private Server. |
| **Investor Tracker** | The hedge fund layer. Tracks each investor's unit NAV, deposits, withdrawals, and equity share. Generates a weekly breakdown pie chart. Private Server only. Lives in `app/investors.py` and `/data/investors.json`. |
| **Tax Reports** | Quarterly Alpaca + RH tax summaries. Fire on the first trading day of Jan, Apr, Jul, and Oct. Jan reports the prior full year; Apr/Jul/Oct report YTD. |

---

### Key rules for the Kimi System

- **Holiday guard**: All Scheduler reports check `was_market_open_today()` (Alpaca calendar API) before firing. No reports on market holidays. Tax Reports additionally guard against double-firing by checking if an earlier trading day already occurred in the same quarter month.
- **Snapshot integrity**: `record_rh_equity_snapshot()` is always called first in daily/weekly jobs, before any report functions, so all RH reports use data captured at the same 4 PM ET instant.
- **RH Session stability**: Never restart the Kimi API process unnecessarily — each restart risks missing the 4 PM ET scheduler tick and requires the catch-up equity snapshot logic to recover.

---

### Repos and infrastructure

| Thing | Location |
|---|---|
| Auto-Trade backend | `github.com/Moses-log/Auto-Trade` |
| KI Site | `github.com/Moses-log/kimi-invest-site` → deploys to `kimiinvest.com` via GitHub Pages |
| Deployed backend | Render (`https://auto-trade-ro8k.onrender.com`) |
| Persistent data | `/data/` on Render disk — 14 JSON files + RH pickle |
| Nightly backup | Private GitHub Gist |
| Public stats endpoint | `GET https://auto-trade-ro8k.onrender.com/public-stats` |

---

### Discord channel routing (abbreviated)

- Alpaca fills → Private Server `#trades`
- RH fills → Private Server `#robinhood`
- Kimi Manager analysis + trades → Private Server `#manager`
- SPY + Manager subscriber signals → KI Server `#kimi-alerts` / `#claude-subscribers`
- Investor breakdown → Private Server (investors webhook)
- RH P&L + snapshot → RH P&L webhook (Private) + subscriber feed (KI Server)
- Session alerts → Private Server `#rh-session`
- Tax summaries → dedicated Alpaca tax and RH tax channels (Private Server)
