# Edgemart Landing Page — Design Spec
Date: 2026-06-15

## Overview

A public marketing landing page for the Edgemart paid Discord signal service. Visitors see live performance proof (stats + chart pulled from Alpaca) and can sign up via Whop. Built as a single HTML file on GitHub Pages, with a new read-only endpoint on the existing Auto-Trade Render server supplying the data.

---

## Goals

- Convince visitors that Edgemart signals are real and worth paying for (live stats, real money, nothing hidden)
- Drive signups through Whop (14-day free trial, then $14.99/month)
- Communicate scarcity: only 15 early access spots, with a live counter
- Be easy to customize later without a framework or build step

---

## Architecture

### Backend — Auto-Trade Render server (existing)

**New endpoint: `GET /public-stats`**
- No authentication required (read-only, no sensitive data exposed)
- Pulls all filled SPY orders from Alpaca using the existing `TradingClient`
- Runs LIFO matching logic (same as the trade history spreadsheet) to compute:
  - `trades` — total completed round-trips
  - `wins` — count of profitable closes
  - `losses` — count of losing closes
  - `win_rate` — wins / trades as a percentage
  - `profit_factor` — gross wins / gross losses
  - `date_range` — `{ from: "Apr 22, 2026", to: "Jun 11, 2026" }` from first/last filled order
  - `cumulative_returns` — array of `{ trade: N, pct: X.XX }` for the chart
  - `spots_remaining` — integer read from `/data/early_access.json`
- Response cached in memory for 1 hour to avoid hammering Alpaca on every page load
- CORS header `Access-Control-Allow-Origin: *` so the GitHub Pages frontend can fetch it

**New endpoint: `POST /whop-webhook`**
- Receives Whop membership webhook events
- On event type `membership.created`: decrement `spots_remaining` in `/data/early_access.json` (floor at 0)
- On event type `membership.deleted`: increment `spots_remaining` in `/data/early_access.json` (cap at 15)
- Webhook secret verified via `X-Whop-Signature` header (HMAC-SHA256)
- New env var: `WHOP_WEBHOOK_SECRET`

**New state file: `/data/early_access.json`**
- Format: `{ "spots_remaining": 15 }`
- Initialized at 15 on first run if file does not exist

### Frontend — `index.html` on GitHub Pages

Single HTML file, no framework, no build step. Uses Chart.js via CDN for the cumulative return chart. Fetches `/public-stats` from the Render server on page load and refreshes every 5 minutes.

New repository: `Moses-log/edgemart-site` (public, GitHub Pages enabled on `main` branch root).

---

## Page Sections

### 1. Hero
- "EDGEMART" — large bold white heading
- "REAL MONEY · EVERY TRADE · PUBLIC" — cyan (`#00e5c8`) uppercase subtitle
- "Closed trades · {date_range.from} – {date_range.to}" — small grey text, populated from API

### 2. Proof Block
Three stat cards (dark card background, uppercase label, large cyan number for WIN RATE and PROFIT FACTOR, white for TRADES):
- TRADES — `{trades}`
- WIN RATE — `{win_rate}%` (cyan)
- PROFIT FACTOR — `{profit_factor}` (cyan)

Below cards: cumulative return line chart (Chart.js)
- X axis: Trade # (0–N)
- Y axis: Cumulative return %
- Line: cyan `#00e5c8`
- Win dots: cyan
- Loss dots: red `#ff4d4d`
- Chart background: dark teal gradient matching screenshot
- "Up days and down days. Nothing hidden." tagline below chart in cyan

### 3. Early Access CTA
- Heading: "EARLY ACCESS"
- Live counter: "X of 15 spots remaining" — updates from `spots_remaining` in API response
- Pricing line: "Free for 14 days, then $14.99 / month"
- Button: "JOIN EDGEMART →" — links to Whop URL (placeholder in code, replaced with real URL before deploy)
- Button style: solid cyan background, dark text, full-width on mobile

### 4. Footer
- Disclaimer: "Past performance ≠ future results. Not financial advice. Trading involves risk of loss."
- Small grey text, centered

---

## Visual Design

| Property | Value |
|---|---|
| Background | `#0d1117` (dark navy) |
| Card background | `#131c27` |
| Accent / cyan | `#00e5c8` |
| Loss red | `#ff4d4d` |
| Heading font | System sans-serif, bold, white |
| Body / label font | System sans-serif, `#8b9ab0` |
| Max page width | 1200px, centered |

Matches the screenshot provided by the user exactly.

---

## What Is NOT Exposed

The `/public-stats` endpoint returns only computed aggregate stats. It does not expose:
- Alpaca API keys or credentials
- Account equity or buying power
- Dollar P&L amounts
- Order IDs or fill details

---

## Environment Variables (new)

| Variable | Purpose |
|---|---|
| `WHOP_WEBHOOK_SECRET` | HMAC secret for verifying Whop webhook payloads |

---

## Hosting

| Component | Host | Cost |
|---|---|---|
| Frontend (`index.html`) | GitHub Pages (`Moses-log/edgemart-site`) | Free |
| Backend (`/public-stats`, `/whop-webhook`) | Existing Render deployment | $0 additional |

---

## Out of Scope

- User authentication on the website
- Showing individual trade history to visitors
- Mobile app
- Email capture / waitlist (can be added later)
