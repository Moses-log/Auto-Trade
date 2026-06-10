"""
Diagnostic script: print the currently registered Discord slash commands.

Run with the same env vars used for registration:
    DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... py scripts/check_commands.py
"""
import httpx
import json
import os
import sys

APP_ID = os.environ.get("DISCORD_APP_ID")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

if not APP_ID or not BOT_TOKEN:
    print("ERROR: Set DISCORD_APP_ID and DISCORD_BOT_TOKEN env vars")
    sys.exit(1)

url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
headers = {"Authorization": f"Bot {BOT_TOKEN}"}

with httpx.Client() as client:
    resp = client.get(url, headers=headers, timeout=15)

if resp.status_code != 200:
    print(f"❌ Failed: {resp.status_code} {resp.text}")
    sys.exit(1)

for cmd in resp.json():
    if cmd["name"] == "report":
        print(json.dumps(cmd, indent=2))
        break
else:
    print("No 'report' command found")
