import json
import urllib.request
import urllib.error
import time

SECRET   = input("Webhook secret: ")
TICKER   = input("Ticker (e.g. SPY): ").upper()
ACTION   = input("Action (buy/sell): ").lower()

payload = {
    "secret": SECRET,
    "ticker": TICKER,
    "action": ACTION,
    "contracts": "1",
    "price": "0",
    "order_id": f"manual_test_{int(time.time())}",
    "market_position": "long" if ACTION == "buy" else "flat",
    "market_position_size": "1" if ACTION == "buy" else "0",
    "prev_market_position": "flat" if ACTION == "buy" else "long",
    "prev_market_position_size": "0" if ACTION == "buy" else "1",
    "timestamp": "2026-01-01T00:00:00Z",
}

body = json.dumps(payload).encode()
req = urllib.request.Request(
    "https://auto-trade-ro8k.onrender.com/webhook",
    data=body,
    headers={"Content-Type": "application/json"},
)

try:
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read().decode())
    print("\nAlpaca:", json.dumps(data["result"].get("orders"), indent=2))
    print("Robinhood:", json.dumps(data["result"].get("robinhood"), indent=2))
except urllib.error.HTTPError as e:
    print("Error", e.code, e.read().decode())
