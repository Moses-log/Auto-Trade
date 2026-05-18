from __future__ import annotations

import base64
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


async def _commit_file(path: str, content: str, message: str) -> None:
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN is not configured — set it in Render environment variables")

    repo = settings.github_repo
    url = f"{_GITHUB_API}/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient() as client:
        get_resp = await client.get(url, headers=headers, timeout=10)
        sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        body: dict = {"message": message, "content": encoded}
        if sha:
            body["sha"] = sha

        put_resp = await client.put(url, headers=headers, json=body, timeout=10)
        if put_resp.status_code not in (200, 201):
            raise RuntimeError(f"GitHub PUT failed: {put_resp.status_code} {put_resp.text}")

    log.info("Committed %s to GitHub (%s)", path, repo)


async def commit_investors_json(content: str) -> None:
    await _commit_file("investors.json", content, "chore: update investors.json via /deposit")


