# Auto-Commit Deposit Design Spec
**Date:** 2026-05-10
**Project:** Moses-log/Auto-Trade

---

## Overview

After a successful `/deposit` call, the server automatically commits the updated `investors.json` to GitHub via the GitHub REST API. No manual `git commit && git push` step is required. GitHub is the source of truth — the deposit is only finalized when the GitHub commit succeeds.

---

## Flow

```
POST /deposit
  │
  ├─ Validate secret
  ├─ Parse DepositRequest
  ├─ Load investors from disk
  ├─ Append new deposit in memory
  ├─ Serialize to JSON string
  │
  ├─ commit_investors_json(content)
  │     ├─ GET /repos/{owner}/{repo}/contents/investors.json → current SHA
  │     └─ PUT /repos/{owner}/{repo}/contents/investors.json → new content + SHA
  │
  ├─ On GitHub failure → return 500, do NOT write to disk
  └─ On GitHub success → write to disk → return 200
```

---

## New Module: `app/github_commit.py`

Single public function:

```python
async def commit_investors_json(content: str) -> None:
    """Commit updated investors.json to GitHub. Raises RuntimeError on failure."""
```

**Implementation:**
1. `GET /repos/{owner}/{repo}/contents/investors.json` with `Authorization: Bearer {token}` — extracts current file SHA
2. `PUT /repos/{owner}/{repo}/contents/investors.json` with base64-encoded content, current SHA, and commit message `"chore: update investors.json via /deposit"`
3. Raises `RuntimeError` if either call returns a non-2xx status or if `GITHUB_TOKEN` is not configured

Uses `httpx.AsyncClient` (already a project dependency). No new dependencies required.

---

## Config Changes

Add to `app/config.py` inside `Settings`:

```python
github_token: Optional[str] = None
github_repo: str = "Moses-log/Auto-Trade"
```

- `GITHUB_TOKEN` — GitHub personal access token with `repo` scope. If not set, `/deposit` returns 500 immediately with a clear error message.
- `GITHUB_REPO` — defaults to `"Moses-log/Auto-Trade"`. Can be overridden via env var if the repo is ever renamed.

---

## `/deposit` Endpoint Changes (`app/main.py`)

Updated sequence after appending the deposit in memory:

```python
content = json.dumps(investors_as_dict, indent=2)  # serialize first
await commit_investors_json(content)                # GitHub first
save_investors(investors)                           # disk only after success
```

On `RuntimeError` from `commit_investors_json`: raise `HTTPException(status_code=500, detail="GitHub commit failed: {error}")`. Nothing is written to disk.

---

## Error Cases

| Condition | Response |
|-----------|----------|
| `GITHUB_TOKEN` not set | 500 — "GitHub token not configured" |
| GitHub GET fails (bad token, network) | 500 — "GitHub commit failed: ..." |
| GitHub PUT fails | 500 — "GitHub commit failed: ..." |
| All succeeds | 200 — deposit returned as before |

---

## Files Changed / Created

| File | Change |
|------|--------|
| `app/github_commit.py` | New — GitHub API commit logic |
| `app/config.py` | Add `github_token` and `github_repo` fields |
| `app/main.py` | Call `commit_investors_json` before `save_investors` in `/deposit` |
| `tests/test_deposit.py` | Add tests for GitHub commit success/failure paths |

---

## Setup Required (Render)

Add environment variable in Render dashboard:
- `GITHUB_TOKEN` = a GitHub personal access token with `repo` scope

Generate at: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → New token → check `repo`.
