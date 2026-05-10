from __future__ import annotations

import base64
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_FILE_PATH = "investors.json"
_COMMIT_MESSAGE = "chore: update investors.json via /deposit"


async def commit_investors_json(content: str) -> None:
    """Commit updated investors.json to GitHub via REST API.

    Raises RuntimeError if the token is missing or either API call fails.
    """
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN is not configured — set it in Render environment variables")

    repo = settings.github_repo
    url = f"{_GITHUB_API}/repos/{repo}/contents/{_FILE_PATH}"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient() as client:
        get_resp = await client.get(url, headers=headers, timeout=10)
        if get_resp.status_code != 200:
            raise RuntimeError(f"GitHub GET failed: {get_resp.status_code} {get_resp.text}")
        sha = get_resp.json()["sha"]

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        put_resp = await client.put(
            url,
            headers=headers,
            json={"message": _COMMIT_MESSAGE, "content": encoded, "sha": sha},
            timeout=10,
        )
        if put_resp.status_code not in (200, 201):
            raise RuntimeError(f"GitHub PUT failed: {put_resp.status_code} {put_resp.text}")

    log.info("investors.json committed to GitHub (%s)", repo)
