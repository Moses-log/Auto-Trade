"""
backup.py — Push critical /data/ files to a private GitHub Gist.

Runs nightly at midnight ET (via APScheduler) and immediately after any
deposit is recorded, since investors.json is the most financially sensitive.

Setup
-----
1. Create a private Gist at gist.github.com (content can be anything).
2. Create a Personal Access Token with the "gist" scope at
   github.com/settings/tokens.
3. Set GITHUB_GIST_TOKEN and GITHUB_GIST_ID in your Render environment vars.

Recovery
--------
  curl -H "Authorization: token $GITHUB_GIST_TOKEN" \
       https://api.github.com/gists/$GITHUB_GIST_ID \
       | python3 -c "import sys,json; d=json.load(sys.stdin)['files']; \
         [open(k,'w').write(v['content']) for k,v in d.items()]"
"""

import logging
import os
from pathlib import Path

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_GIST_API = "https://api.github.com/gists"

# Ordered by financial importance — investors.json first.
_BACKUP_FILES: list[tuple[str, Path]] = [
    ("investors.json",            Path(os.getenv("INVESTORS_FILE",           "/data/investors.json"))),
    ("claude_rebalance_log.json", Path(os.getenv("REBALANCE_LOG_FILE",       "/data/claude_rebalance_log.json"))),
    ("rh_equity_history.json",    Path(os.getenv("RH_EQUITY_HISTORY_FILE",   "/data/rh_equity_history.json"))),
    ("rh_trade_record.json",      Path(os.getenv("RH_TRADE_RECORD_FILE",     "/data/rh_trade_record.json"))),
    ("claude_portfolio.json",     Path(os.getenv("CLAUDE_PORTFOLIO_FILE",    "/data/claude_portfolio.json"))),
    ("trade_record.json",         Path(os.getenv("TRADE_RECORD_FILE",        "/data/trade_record.json"))),
    ("leverage_entry.json",       Path(os.getenv("LEVERAGE_STATE_FILE",      "/data/leverage_entry.json"))),
    ("kimi_trades.json",           Path("/data/kimi_trades.json")),
    ("early_access.json",          Path("/data/early_access.json")),
]


async def push_backup() -> dict:
    """Read all critical /data/ files and PATCH them into the configured Gist.

    Returns {"ok": True, "files_backed_up": [...]} on success, or
    {"ok": False, "error": "..."} on failure or misconfiguration.
    Silently skips files that don't yet exist on disk.
    """
    token = settings.github_gist_token
    gist_id = settings.github_gist_id

    if not token or not gist_id:
        log.debug("Backup skipped — GITHUB_GIST_TOKEN or GITHUB_GIST_ID not configured")
        return {"ok": False, "error": "not_configured"}

    files: dict[str, dict] = {}
    for name, path in _BACKUP_FILES:
        if not path.exists():
            continue
        try:
            files[name] = {"content": path.read_text(encoding="utf-8")}
        except Exception as exc:
            log.warning("Backup: could not read %s: %s", path, exc)

    if not files:
        log.warning("Backup: no /data/ files found — nothing pushed to Gist")
        return {"ok": False, "error": "no_files"}

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(
                f"{_GIST_API}/{gist_id}",
                json={"files": files},
                headers=headers,
            )
        resp.raise_for_status()
        names = list(files.keys())
        log.info("Backup: pushed %d files to Gist %s: %s", len(names), gist_id, names)
        return {"ok": True, "files_backed_up": names}
    except Exception as exc:
        log.error("Backup: Gist push failed: %s", exc)
        return {"ok": False, "error": str(exc)}
