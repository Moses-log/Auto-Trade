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

COMMANDS: list = [
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
                "type": 1,
                "name": "alpaca",
                "description": "Alpaca account P&L report",
                "options": [
                    {
                        "name": "type",
                        "description": "Report period",
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
                            {"name": "Investor Breakdown", "value": "investors"},
                        ],
                    }
                ],
            },
            {
                "type": 1,
                "name": "robinhood",
                "description": "Robinhood Agentic account P&L report",
                "options": [
                    {
                        "name": "type",
                        "description": "Report period",
                        "type": 3,
                        "required": True,
                        "choices": [
                            {"name": "Daily", "value": "daily"},
                            {"name": "Weekly", "value": "weekly"},
                            {"name": "Monthly", "value": "monthly"},
                            {"name": "Year to Date", "value": "ytd"},
                            {"name": "1 Year", "value": "1year"},
                            {"name": "All Time", "value": "alltime"},
                        ],
                    }
                ],
            },
        ],
    },
    {
        "name": "tax",
        "description": "Realized gain/loss summary for IRS tax reporting",
        "options": [
            {
                "type": 1,
                "name": "alpaca",
                "description": "Alpaca fund realized gains/losses with investor breakdown",
                "options": [
                    {
                        "name": "year",
                        "description": "Tax year (defaults to current year)",
                        "type": 4,
                        "required": False,
                    },
                ],
            },
            {
                "type": 1,
                "name": "robinhood",
                "description": "Robinhood algorithmic trading gains/losses",
                "options": [
                    {
                        "name": "year",
                        "description": "Tax year (defaults to current year)",
                        "type": 4,
                        "required": False,
                    },
                ],
            },
        ],
    },
    {
        "name": "status",
        "description": "Show Alpaca + Robinhood account status and open positions",
        "options": [],
    },
    {
        "name": "positions",
        "description": "List all open positions with real-time P&L",
        "options": [
            {
                "name": "broker",
                "description": "Which broker to query (default: both)",
                "type": 3,
                "required": False,
                "choices": [
                    {"name": "Both", "value": "both"},
                    {"name": "Alpaca", "value": "alpaca"},
                    {"name": "Robinhood", "value": "robinhood"},
                ],
            },
        ],
    },
    {
        "name": "close",
        "description": "Close a position by ticker symbol",
        "options": [
            {
                "name": "ticker",
                "description": "Ticker symbol (e.g. SPY)",
                "type": 3,
                "required": True,
            },
            {
                "name": "broker",
                "description": "Which broker to close on (default: both)",
                "type": 3,
                "required": False,
                "choices": [
                    {"name": "Both", "value": "both"},
                    {"name": "Alpaca", "value": "alpaca"},
                    {"name": "Robinhood", "value": "robinhood"},
                ],
            },
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
