"""
One-time script to register Discord slash commands.

Run once after setting DISCORD_APP_ID and DISCORD_BOT_TOKEN:
    DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... py scripts/register_commands.py
"""
import httpx
import os
import sys

APP_ID = os.environ.get("DISCORD_APP_ID")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

if not APP_ID or not BOT_TOKEN:
    print("ERROR: Set DISCORD_APP_ID and DISCORD_BOT_TOKEN env vars")
    sys.exit(1)

COMMANDS = [
    {
        "name": "deposit",
        "description": "Record an investor cash deposit",
        "options": [
            {
                "name": "investor",
                "description": "Investor name",
                "type": 3,
                "required": True,
            },
            {
                "name": "amount",
                "description": "Deposit amount in USD",
                "type": 10,
                "required": True,
            },
            {
                "name": "spy_price",
                "description": "SPY entry price (fetched live if omitted)",
                "type": 10,
                "required": False,
            },
        ],
    },
    {
        "name": "withdraw",
        "description": "Record an investor cash withdrawal",
        "options": [
            {
                "name": "investor",
                "description": "Investor name",
                "type": 3,
                "required": True,
            },
            {
                "name": "amount",
                "description": "Withdrawal amount in USD",
                "type": 10,
                "required": True,
            },
        ],
    },
    {
        "name": "report",
        "description": "Trigger a P&L report to Discord",
        "options": [
            {
                "name": "type",
                "description": "Report type",
                "type": 3,
                "required": True,
                "choices": [
                    {"name": "Daily", "value": "daily"},
                    {"name": "Weekly", "value": "weekly"},
                    {"name": "Monthly", "value": "monthly"},
                    {"name": "Year to Date", "value": "ytd"},
                    {"name": "1 Year", "value": "1year"},
                    {"name": "All Time", "value": "alltime"},
                    {"name": "Daily & Weekly", "value": "both"},
                ],
            }
        ],
    },
]

url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
headers = {"Authorization": f"Bot {BOT_TOKEN}"}

with httpx.Client() as client:
    resp = client.put(url, json=COMMANDS, headers=headers, timeout=15)

if resp.status_code in (200, 201):
    print(f"✅ Registered {len(COMMANDS)} commands successfully")
    for cmd in resp.json():
        print(f"  /{cmd['name']}")
else:
    print(f"❌ Failed: {resp.status_code} {resp.text}")
    sys.exit(1)
