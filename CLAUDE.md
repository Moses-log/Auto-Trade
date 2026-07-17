# Kimi System — Claude Context

## The Kimi System: Naming Map

Every part of Kimi System got canonical name. Use these name in talk,
Claude know exact which part you mean, no more explain needed.

---

### Strategies — the brains that make trading decisions

| Name | What it is |
|---|---|
| **Kimi SPY** | TradingView-trigger auto DCA on SPY. Alpaca primary execution; RH mirror every trade. |
| **Kimi Manager** | Auto monthly Claude (Opus) rebalance of long-term equity portfolio. RH only. Run 1st of month, 9:35 AM ET. |
| **Kimi Autopilot** | Manual signal from @theaiportfolios on X, forward to RH for execution. |

---

### Brokers — where trades actually execute

Each broker got two Discord presence: one in KI Server (subscriber), one in Private Server (owner-only). Use name below to refer.

| Name | Broker | What it contains |
|---|---|---|
| **Alpaca KI** | Alpaca | Kimi SPY trade signal/callout + Trades P&L summary. Visible to KI Server subscriber. |
| **Alpaca Private** | Alpaca | Everything in Alpaca KI, plus full P&L report (daily, weekly, monthly, yearly), SPY comparison chart, fill notification, tax summary. Owner-only. |
| **RH KI** | Robinhood | Callout only — Kimi Manager rebalance decision + Kimi Autopilot signal. Subscriber see what system decide, not execution detail or P&L. |
| **RH Private** | Robinhood | Everything: fill notification, full RH P&L report (daily/weekly/monthly/yearly) with chart, Friday portfolio pie chart snapshot, Investor Tracker equity breakdown, RH Session alert, tax summary. Owner-only. |

---

### Servers — the Discord audiences

| Name | What it is |
|---|---|
| **KI Server** | Paid Kimi Invest Discord. Subscriber (access via kimiinvest.com/Whop) see SPY signal, Manager callout, RH alert, politician tracker, vetted investor alert. |
| **Private Server** | Owner-only Discord. Receive all raw execution fill, P&L report, investor breakdown, session alert, tax summary — full detail, no filter. |

Both server receive signal from all three strategy, but different detail level.

---

### The Site — the public face

| Name | What it is |
|---|---|
| **KI Site** | kimiinvest.com — public marketing site + live transparency dashboard. Page: index, trades, portfolio, stats. Pull from public-stats endpoint on Kimi API. |

---

### The Engine — the backend that runs everything

| Name | What it is |
|---|---|
| **Kimi API** | FastAPI server deploy on Render. Handle all webhook (TradingView), Discord slash command, HTTP endpoint, route signal to broker. Entry point: `app/main.py`. |
| **Scheduler** | APScheduler cron job inside Kimi API. Drive all auto behavior: P&L report, Kimi Manager monthly rebalance, RH keep-alive, nightly backup. Define in `app/scheduler.py`. |
| **RH Session** | Robinhood auth manager. Handle initial login (via pickle), keep-alive refresh every 1–2 day between 1–5 AM ET, re-auth flow when session expire. |
| **Gist Backup** | Nightly push of all `/data/` JSON file to private GitHub Gist. Fire midnight ET via Scheduler. |

---

### Reports — automated outputs from the Scheduler

| Name | What it is |
|---|---|
| **Alpaca Reports** | Daily + weekly Alpaca P&L with SPY comparison + chart. Send to Private Server, 4 PM ET, trading day. Monthly + yearly fire last trading day of period. |
| **RH Reports** | Daily + weekly RH P&L with SPY comparison. Also include Friday portfolio pie chart (position + valuation rating). Send to both KI Server + Private Server. |
| **Investor Tracker** | Hedge fund layer. Track each investor unit NAV, deposit, withdrawal, equity share. Generate weekly breakdown pie chart. Private Server only. Live in `app/investors.py` and `/data/investors.json`. |
| **Tax Reports** | Quarterly Alpaca + RH tax summary. Fire first trading day of Jan, Apr, Jul, Oct. Jan report prior full year; Apr/Jul/Oct report YTD. |

---

### Thesis Memory — how Kimi carries reasoning between rebalances

| Name | What it is |
|---|---|
| **Living Thesis** | How Kimi keep thesis continuity between deep rebalance. Each **Kimi Manager** rebalance write durable per-ticker ANCHOR thesis (full 5-section research). Each **Kimi Inspection** that act on a holding append dated one-line update *beneath* the anchor — layer on, never replace — so acted holding keep foundational research plus its evolution. Kimi Inspection read Living Thesis as prior-thesis context each week. Keep last `_MAX_THESIS_DELTAS` (now **4** ≈ one rebalance cycle of weekly inspection) update per ticker, oldest-to-newest; note predating anchor is stale, ignored; ticker sold then re-bought self-heal from fresh rebalance anchor. Live in `_build_prior_thesis_map`, `app/claude_inspection.py`. |

---

### Key rules for the Kimi System

- **Holiday guard**: All Scheduler report check `was_market_open_today()` (Alpaca calendar API) before fire. No report on market holiday. Tax Reports also guard against double-fire by check if earlier trading day already happen same quarter month.
- **Snapshot integrity**: `record_rh_equity_snapshot()` always call first in daily/weekly job, before any report function, so all RH report use data capture at same 4 PM ET instant.
- **RH Session stability**: Never restart Kimi API process unless need — each restart risk miss 4 PM ET scheduler tick, need catch-up equity snapshot logic to recover.

---

### Repos and infrastructure

| Thing | Location |
|---|---|
| Auto-Trade backend | `github.com/Moses-log/Auto-Trade` |
| KI Site | `github.com/Moses-log/kimi-invest-site` → deploy to `kimiinvest.com` via GitHub Pages |
| Deployed backend | Render (`https://auto-trade-ro8k.onrender.com`) |
| Persistent data | `/data/` on Render disk — 14 JSON file + RH pickle |
| Nightly backup | Private GitHub Gist |
| Public stats endpoint | `GET https://auto-trade-ro8k.onrender.com/public-stats` |

---

### Discord channel routing (abbreviated)

- Alpaca fill → Private Server `#trades`
- RH fill → Private Server `#robinhood`
- Kimi Manager analysis + trade → Private Server `#manager`
- SPY + Manager subscriber signal → KI Server `#kimi-alerts` / `#claude-subscribers`
- Investor breakdown → Private Server (investor webhook)
- RH P&L + snapshot → RH P&L webhook (Private) + subscriber feed (KI Server)
- Session alert → Private Server `#rh-session`
- Tax summary → dedicate Alpaca tax + RH tax channel (Private Server)