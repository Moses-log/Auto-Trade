# TradingView → Alpaca Webhook

A production-ready Python / FastAPI service that receives TradingView strategy
alerts and executes trades through the Alpaca brokerage API.

---

## Project structure

```
tradingview-alpaca-webhook/
├── app/
│   ├── main.py              # FastAPI app, /webhook and /health endpoints
│   ├── config.py            # All settings loaded from environment variables
│   ├── models.py            # Pydantic model for TradingView alert payloads
│   ├── security.py          # Shared-secret validation (constant-time)
│   ├── idempotency.py       # Duplicate-alert suppression with TTL store
│   ├── logging_config.py    # Structured JSON logging
│   ├── notifications.py     # Discord / Telegram notification stubs
│   └── trading/
│       ├── alpaca_client.py # Alpaca API wrapper + retry logic
│       └── order_logic.py   # TradingView action → Alpaca order translation
├── tests/
│   ├── test_webhook.py      # pytest test suite
│   └── sample_payloads.json # One sample payload per action type
├── .env.example             # Template — copy to .env and fill in values
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Quick start

### 1 — Prerequisites

- Python 3.11+
- A free [Alpaca paper trading account](https://app.alpaca.markets)
- Your Alpaca **Paper** API key + secret

### 2 — Clone and install

```bash
git clone <your-repo>
cd tradingview-alpaca-webhook
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3 — Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in:

```env
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
WEBHOOK_SECRET=replace_me_with_a_long_random_string
```

Generate a strong secret:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 4 — Run locally

```bash
uvicorn app.main:app --reload --port 8000
```

The server is now listening on `http://localhost:8000`.

### 5 — Verify

```bash
curl http://localhost:8000/health
# → {"status":"ok","uptime_s":1.2,"paper":true}
```

---

## Local testing with ngrok

TradingView requires a publicly reachable HTTPS URL. Use ngrok to tunnel to
your local server.

### Install ngrok

Download from https://ngrok.com/download or:

```bash
# macOS
brew install ngrok

# Linux
snap install ngrok
```

### Start a tunnel

```bash
ngrok http 8000
```

ngrok prints something like:

```
Forwarding  https://abc123.ngrok-free.app -> http://localhost:8000
```

Copy the `https://...` URL — that is your TradingView webhook URL.

### Send a test alert

```bash
curl -s -X POST https://abc123.ngrok-free.app/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "secret":   "YOUR_WEBHOOK_SECRET",
    "ticker":   "AAPL",
    "action":   "buy",
    "contracts":"10",
    "price":    "189.45",
    "order_id": "manual_test_001",
    "timestamp":"2024-01-15T14:30:00Z"
  }'
```

Expected response:

```json
{
  "status": "ok",
  "result": {
    "action": "buy",
    "ticker": "AAPL",
    "orders": [{
      "alpaca_order_id": "...",
      "symbol": "AAPL",
      "side": "buy",
      "qty": "10",
      "type": "market",
      "status": "accepted"
    }]
  }
}
```

Check your [Alpaca paper trading dashboard](https://app.alpaca.markets) to
confirm the order appeared.

---

## TradingView setup

### Alert message template

Paste this exactly into the TradingView **Alert → Message** field:

```json
{
  "secret":                   "YOUR_WEBHOOK_SECRET",
  "ticker":                   "{{ticker}}",
  "action":                   "{{strategy.order.action}}",
  "contracts":                "{{strategy.order.contracts}}",
  "price":                    "{{close}}",
  "order_id":                 "{{strategy.order.id}}",
  "market_position":          "{{strategy.market_position}}",
  "market_position_size":     "{{strategy.market_position_size}}",
  "prev_market_position":     "{{strategy.prev_market_position}}",
  "prev_market_position_size":"{{strategy.prev_market_position_size}}",
  "timestamp":                "{{timenow}}"
}
```

Replace `YOUR_WEBHOOK_SECRET` with the value from your `.env` file.

### Alert settings

| Field          | Value                                      |
|----------------|--------------------------------------------|
| Webhook URL    | `https://your-server.com/webhook`          |
| Message        | The JSON template above                    |
| Expiration     | Open-ended (or match your strategy)        |

### Strategy alert actions

TradingView's `{{strategy.order.action}}` automatically emits `buy` or `sell`.
For the close/reverse actions, use custom `alert()` calls in your Pine Script:

```pine
// Example Pine Script snippets

// Close long explicitly
if (exitLongCondition)
    strategy.close("Long", comment="close_long")
    alert('{"secret":"YOUR_SECRET","ticker":"' + syminfo.ticker + '","action":"close_long","contracts":"' + str.tostring(strategy.position_size) + '","price":"' + str.tostring(close) + '","timestamp":"' + str.tostring(timenow) + '"}', alert.freq_once_per_bar_close)

// Reverse to long
if (reverseLongCondition)
    strategy.entry("Long", strategy.long, comment="reverse_to_long")
    alert('{"secret":"YOUR_SECRET","ticker":"' + syminfo.ticker + '","action":"reverse_to_long","contracts":"10","price":"' + str.tostring(close) + '","timestamp":"' + str.tostring(timenow) + '"}', alert.freq_once_per_bar_close)
```

---

## Supported actions

| Action            | What happens in Alpaca                                   |
|-------------------|----------------------------------------------------------|
| `buy`             | Market BUY for `contracts` shares                        |
| `sell`            | Market SELL for `contracts` shares                       |
| `close_long`      | Close entire long position (ignores `contracts`)         |
| `close_short`     | Close entire short position / buy-to-cover               |
| `reverse_to_long` | Close short (if any) → Market BUY `contracts` shares     |
| `reverse_to_short`| Close long (if any) → Market SELL `contracts` shares     |

---

## Running tests

```bash
pytest tests/ -v
```

No real Alpaca calls are made — the Alpaca client is mocked.

---

## Deployment

### Option A — Render (easiest free tier)

1. Push your project to GitHub.
2. Go to [render.com](https://render.com) → New → Web Service.
3. Connect your repo.
4. Set:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add all environment variables from `.env.example` in the Render dashboard.
6. Deploy. Render gives you a public HTTPS URL automatically.

### Option B — Railway

1. Push to GitHub.
2. New project → Deploy from GitHub repo.
3. Add environment variables in the Railway Variables panel.
4. Railway auto-detects Python; set the start command to:
   ```
   uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```

### Option C — Docker on a VPS (DigitalOcean, Hetzner, Linode)

```bash
# On your VPS
git clone <your-repo>
cd tradingview-alpaca-webhook
cp .env.example .env
# Edit .env with your real values
nano .env

docker compose up -d
```

Add an nginx reverse proxy with a Let's Encrypt SSL certificate so
TradingView can reach `https://your-domain.com/webhook`.

Minimal nginx config:

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Real-IP         $remote_addr;
    }
}
```

---

## Switching to live trading

1. Create a Live account at Alpaca (requires identity verification).
2. Generate Live API keys.
3. Update `.env`:
   ```env
   ALPACA_API_KEY=your_live_key
   ALPACA_SECRET_KEY=your_live_secret
   ALPACA_BASE_URL=https://api.alpaca.markets
   ```
4. **Test thoroughly on paper first.** Verify every action type works as
   expected before pointing live alerts at a live account.

---

## Fractional shares

Fractional share trading is **disabled by default** (`ALLOW_FRACTIONAL_SHARES=false`).

When disabled, `contracts` is floored to the nearest whole number. A value
that rounds to zero raises a `422` error so you catch misconfigured alerts
early rather than placing zero-share orders.

Enable only if:
- Your Alpaca account has fractional trading enabled, AND
- The symbols you trade support fractional orders on Alpaca.

---

## Notifications

Set either or both in `.env` to receive a message on every trade and error:

- `DISCORD_WEBHOOK_URL` — Discord channel webhook URL
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — Telegram bot

Leave them blank to disable silently.

---

## Security notes

- The shared secret is validated with `hmac.compare_digest` to prevent
  timing-oracle attacks.
- Never commit your `.env` file. It is in `.gitignore` by default.
- The Swagger UI (`/docs`) is disabled in production. Re-enable in `main.py`
  by removing the `docs_url=None` argument while developing.
- Consider IP-allowlisting TradingView's webhook IPs at your reverse proxy or
  firewall level for additional defence-in-depth.
