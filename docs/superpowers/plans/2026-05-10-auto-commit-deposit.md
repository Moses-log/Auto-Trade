# Auto-Commit Deposit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a successful `/deposit` call, automatically commit the updated `investors.json` to GitHub so no manual git step is required.

**Architecture:** A new `app/github_commit.py` module handles the GitHub API call (GET current SHA → PUT new content). The `/deposit` endpoint calls it before writing to disk — if the GitHub commit fails, the disk is never written and a 500 is returned. A new `serialize_investors()` helper in `app/investors.py` produces the JSON string shared by both the GitHub commit and the disk write.

**Tech Stack:** Python, FastAPI, httpx (already a dependency), GitHub REST API, pytest, pytest-asyncio

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `app/investors.py` | Modify | Add `serialize_investors()` helper; refactor `save_investors` to use it |
| `app/config.py` | Modify | Add `github_token` and `github_repo` config fields |
| `app/github_commit.py` | Create | GitHub REST API commit logic |
| `app/main.py` | Modify | Call `commit_investors_json` before `save_investors` in `/deposit` |
| `tests/test_deposit.py` | Modify | Patch `commit_investors_json` in existing happy-path tests; add GitHub failure/success tests |
| `tests/test_github_commit.py` | Create | Unit tests for `commit_investors_json` |

---

### Task 1: `app/investors.py` — add `serialize_investors()`

**Files:**
- Modify: `app/investors.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_investors.py`:

```python
def test_serialize_investors_returns_valid_json():
    from app.investors import Deposit, Investor, serialize_investors
    import json
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")])
    ]
    result = serialize_investors(investors)
    data = json.loads(result)
    assert data["investors"][0]["name"] == "Moses"
    assert data["investors"][0]["deposits"][0]["amount"] == 300.0
    assert data["investors"][0]["deposits"][0]["entry_spy"] == 707.116


def test_serialize_investors_output_matches_save_investors(tmp_path):
    from app.investors import Deposit, Investor, save_investors, serialize_investors
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")])
    ]
    path = tmp_path / "investors.json"
    save_investors(investors, path=path)
    assert path.read_text(encoding="utf-8") == serialize_investors(investors)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_investors.py::test_serialize_investors_returns_valid_json -v
```

Expected: `ImportError: cannot import name 'serialize_investors'`

- [ ] **Step 3: Add `serialize_investors` to `app/investors.py` and refactor `save_investors`**

Replace the existing `save_investors` function in `app/investors.py` with:

```python
def serialize_investors(investors: list[Investor]) -> str:
    data = {
        "investors": [
            {
                "name": inv.name,
                "deposits": [
                    {"amount": d.amount, "entry_spy": d.entry_spy, "date": d.date}
                    for d in inv.deposits
                ],
            }
            for inv in investors
        ]
    }
    return json.dumps(data, indent=2)


def save_investors(investors: list[Investor], path: Path = INVESTORS_FILE) -> None:
    path.write_text(serialize_investors(investors), encoding="utf-8")
```

- [ ] **Step 4: Run all tests to verify they pass**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
cd C:\Users\moses\Auto-Trade && git add app/investors.py tests/test_investors.py && git commit -m "feat: add serialize_investors() helper to investors.py"
```

---

### Task 2: `app/config.py` — add GitHub config fields

**Files:**
- Modify: `app/config.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investors.py`:

```python
def test_config_github_token_defaults_to_none():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x")
    assert s.github_token is None


def test_config_github_repo_defaults_to_auto_trade():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x")
    assert s.github_repo == "Moses-log/Auto-Trade"


def test_config_github_token_accepts_value():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x", github_token="ghp_abc123")
    assert s.github_token == "ghp_abc123"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_investors.py::test_config_github_token_defaults_to_none -v
```

Expected: `AttributeError` or `ValidationError` — field does not exist on `Settings`.

- [ ] **Step 3: Add fields to `app/config.py`**

Inside the `Settings` class in `app/config.py`, add after the existing `discord_investors_webhook_url` field:

```python
github_token: Optional[str] = None
github_repo: str = "Moses-log/Auto-Trade"
```

`Optional` is already imported in `config.py`.

- [ ] **Step 4: Run all tests to verify they pass**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
cd C:\Users\moses\Auto-Trade && git add app/config.py tests/test_investors.py && git commit -m "feat: add github_token and github_repo to Settings"
```

---

### Task 3: `app/github_commit.py` — GitHub API commit logic

**Files:**
- Create: `app/github_commit.py`
- Create: `tests/test_github_commit.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_commit.py`:

```python
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

CONTENT = '{"investors": []}'


@pytest.mark.asyncio
async def test_commit_raises_when_github_token_not_set():
    with patch("app.github_commit.settings") as mock_settings:
        mock_settings.github_token = None
        from app.github_commit import commit_investors_json
        with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
            await commit_investors_json(CONTENT)


@pytest.mark.asyncio
async def test_commit_raises_on_get_failure():
    with patch("app.github_commit.settings") as mock_settings:
        mock_settings.github_token = "fake-token"
        mock_settings.github_repo = "Moses-log/Auto-Trade"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=MagicMock(status_code=401, text="Unauthorized"))
            from app.github_commit import commit_investors_json
            with pytest.raises(RuntimeError, match="GitHub GET failed"):
                await commit_investors_json(CONTENT)


@pytest.mark.asyncio
async def test_commit_raises_on_put_failure():
    with patch("app.github_commit.settings") as mock_settings:
        mock_settings.github_token = "fake-token"
        mock_settings.github_repo = "Moses-log/Auto-Trade"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            get_resp = MagicMock(status_code=200)
            get_resp.json = MagicMock(return_value={"sha": "abc123"})
            mock_client.get = AsyncMock(return_value=get_resp)
            mock_client.put = AsyncMock(return_value=MagicMock(status_code=422, text="Unprocessable"))
            from app.github_commit import commit_investors_json
            with pytest.raises(RuntimeError, match="GitHub PUT failed"):
                await commit_investors_json(CONTENT)


@pytest.mark.asyncio
async def test_commit_succeeds_and_calls_put_with_base64_content():
    with patch("app.github_commit.settings") as mock_settings:
        mock_settings.github_token = "fake-token"
        mock_settings.github_repo = "Moses-log/Auto-Trade"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            get_resp = MagicMock(status_code=200)
            get_resp.json = MagicMock(return_value={"sha": "abc123"})
            mock_client.get = AsyncMock(return_value=get_resp)
            mock_client.put = AsyncMock(return_value=MagicMock(status_code=201, text="Created"))
            from app.github_commit import commit_investors_json
            await commit_investors_json(CONTENT)  # must not raise
        mock_client.put.assert_called_once()
        put_kwargs = mock_client.put.call_args[1]
        import base64
        assert put_kwargs["json"]["sha"] == "abc123"
        assert put_kwargs["json"]["content"] == base64.b64encode(CONTENT.encode()).decode()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_github_commit.py -v
```

Expected: `ModuleNotFoundError` — `app.github_commit` does not exist yet.

- [ ] **Step 3: Create `app/github_commit.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_github_commit.py -v
```

Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
cd C:\Users\moses\Auto-Trade && git add app/github_commit.py tests/test_github_commit.py && git commit -m "feat: add github_commit.py with commit_investors_json()"
```

---

### Task 4: `app/main.py` — wire up auto-commit in `/deposit`

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_deposit.py`

- [ ] **Step 1: Update `tests/test_deposit.py`**

Four existing happy-path tests need `commit_investors_json` patched (it now runs before `save_investors`). Replace the entire file with:

```python
import os
import pytest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from fastapi.testclient import TestClient

from app.investors import Deposit, Investor
from app.main import app

client = TestClient(app)
TEST_SECRET = "MY_SHARED_SECRET"


def _initial_investors():
    return [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")])
    ]


def test_deposit_rejects_wrong_secret():
    response = client.post("/deposit", json={
        "secret": "wrong-secret",
        "investor": "Moses",
        "amount": 500.0,
    })
    assert response.status_code == 401


def test_deposit_rejects_malformed_json():
    response = client.post(
        "/deposit",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_deposit_appends_to_existing_investor():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.commit_investors_json", new_callable=AsyncMock):
            with patch("app.main.save_investors"):
                with patch("app.main.get_latest_price", return_value=580.0):
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "Moses",
                        "amount": 500.0,
                    })
    assert response.status_code == 200
    data = response.json()
    assert data["investor"] == "Moses"
    assert len(data["deposits"]) == 2
    assert data["deposits"][1]["amount"] == 500.0
    assert data["deposits"][1]["entry_spy"] == 580.0


def test_deposit_uses_provided_spy_price_and_skips_alpaca_call():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.commit_investors_json", new_callable=AsyncMock):
            with patch("app.main.save_investors"):
                with patch("app.main.get_latest_price") as mock_price:
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "Moses",
                        "amount": 500.0,
                        "spy_price": 595.0,
                    })
    assert response.status_code == 200
    mock_price.assert_not_called()
    assert response.json()["deposits"][1]["entry_spy"] == 595.0


def test_deposit_creates_new_investor_when_name_not_found():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.commit_investors_json", new_callable=AsyncMock):
            with patch("app.main.save_investors"):
                with patch("app.main.get_latest_price", return_value=580.0):
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "Alice",
                        "amount": 1000.0,
                    })
    assert response.status_code == 200
    data = response.json()
    assert data["investor"] == "Alice"
    assert len(data["deposits"]) == 1
    assert data["deposits"][0]["amount"] == 1000.0


def test_deposit_matches_investor_name_case_insensitively():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.commit_investors_json", new_callable=AsyncMock):
            with patch("app.main.save_investors"):
                with patch("app.main.get_latest_price", return_value=580.0):
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "moses",
                        "amount": 200.0,
                    })
    assert response.status_code == 200
    assert response.json()["investor"] == "Moses"


def test_deposit_returns_502_when_spy_price_unavailable():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.get_latest_price", return_value=None):
            response = client.post("/deposit", json={
                "secret": TEST_SECRET,
                "investor": "Moses",
                "amount": 500.0,
            })
    assert response.status_code == 502


def test_deposit_rejects_zero_amount():
    response = client.post("/deposit", json={
        "secret": TEST_SECRET,
        "investor": "Moses",
        "amount": 0.0,
    })
    assert response.status_code == 422


def test_deposit_returns_500_and_skips_disk_write_when_github_fails():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.get_latest_price", return_value=580.0):
            with patch("app.main.commit_investors_json", new_callable=AsyncMock,
                       side_effect=RuntimeError("GitHub GET failed: 401 Unauthorized")):
                with patch("app.main.save_investors") as mock_save:
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "Moses",
                        "amount": 500.0,
                    })
    assert response.status_code == 500
    mock_save.assert_not_called()


def test_deposit_commits_to_github_before_writing_disk():
    call_order = []
    async def fake_commit(content):
        call_order.append("github")
    def fake_save(investors):
        call_order.append("disk")

    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.get_latest_price", return_value=580.0):
            with patch("app.main.commit_investors_json", side_effect=fake_commit):
                with patch("app.main.save_investors", side_effect=fake_save):
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "Moses",
                        "amount": 500.0,
                    })
    assert response.status_code == 200
    assert call_order == ["github", "disk"]
```

- [ ] **Step 2: Run tests to verify the two new tests fail, existing ones still pass**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_deposit.py -v
```

Expected: `test_deposit_returns_500_and_skips_disk_write_when_github_fails` and `test_deposit_commits_to_github_before_writing_disk` FAIL (endpoint not yet wired); the existing 8 tests may fail too because `commit_investors_json` is now patched but not called yet.

- [ ] **Step 3: Update `app/main.py`**

Add to the imports at the top of `app/main.py`:

```python
from app.github_commit import commit_investors_json
from app.investors import Deposit, Investor, load_investors, save_investors, serialize_investors
```

Replace the existing `save_investors(investors)` call inside the `deposit()` function (line 278) with:

```python
    content = serialize_investors(investors)
    try:
        await commit_investors_json(content)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    save_investors(investors)
```

The full updated bottom of the `deposit()` function looks like:

```python
    new_deposit = Deposit(amount=req.amount, entry_spy=spy_price, date=date.today().isoformat())

    if match is None:
        match = Investor(name=req.investor, deposits=[new_deposit])
        investors.append(match)
    else:
        match.deposits.append(new_deposit)

    content = serialize_investors(investors)
    try:
        await commit_investors_json(content)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    save_investors(investors)

    return {
        "investor": match.name,
        "deposits": [
            {"amount": d.amount, "entry_spy": d.entry_spy, "date": d.date}
            for d in match.deposits
        ],
    }
```

- [ ] **Step 4: Run deposit tests — all 10 must pass**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest tests/test_deposit.py -v
```

Expected: 10 tests PASSED.

- [ ] **Step 5: Run the full test suite**

```bash
cd C:\Users\moses\Auto-Trade && py -m pytest -v
```

Expected: all tests PASSED.

- [ ] **Step 6: Commit**

```bash
cd C:\Users\moses\Auto-Trade && git add app/main.py tests/test_deposit.py && git commit -m "feat: auto-commit investors.json to GitHub on /deposit"
```

---

## Done

Once deployed to Render, add `GITHUB_TOKEN` to the service's environment variables (Render dashboard → your service → Environment). Generate it at: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → New token → check `repo`.

Every `/deposit` call will now automatically commit `investors.json` to GitHub. No manual git step needed.
