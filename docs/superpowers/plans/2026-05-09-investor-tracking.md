# Investor Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-investor equity tracking to the shared Alpaca portfolio, reporting each person's stake as SPY moves, with daily/weekly Discord breakdowns and a `/deposit` endpoint for recording new deposits.

**Architecture:** A new `app/investors.py` module owns all investor data (loaded from `investors.json` at repo root), equity math, and Discord message formatting. Existing `app/pnl.py` and `app/scheduler.py` are extended to call `send_investor_report()` after each daily/weekly report. A new `POST /deposit` endpoint on the existing FastAPI server handles adding deposits and new members.

**Tech Stack:** Python, FastAPI, Pydantic v2, APScheduler, httpx, pytest, pytest-asyncio

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `investors.json` | Create (repo root) | Source of truth for all investor deposit records |
| `app/investors.py` | Create | Investor data model, load/save, equity math, Discord formatting |
| `app/config.py` | Modify | Add `discord_investors_webhook_url` optional field |
| `app/models.py` | Modify | Add `DepositRequest` Pydantic model |
| `app/notifications.py` | Modify | Add `notify_investors()` with channel routing |
| `app/pnl.py` | Modify | Add `send_investor_report()` async function |
| `app/scheduler.py` | Modify | Register two investor breakdown cron jobs |
| `app/main.py` | Modify | Register `POST /deposit` endpoint |
| `tests/test_investors.py` | Create | Unit tests for all of the above |
| `tests/test_deposit.py` | Create | Integration tests for `POST /deposit` |

---

### Task 1: Create `investors.json`

**Files:**
- Create: `investors.json` (repo root)

- [ ] **Step 1: Create the file**

```json
{
  "investors": [
    {
      "name": "Moses",
      "deposits": [
        {"amount": 300, "entry_spy": 707.116, "date": "2026-05-09"}
      ]
    },
    {
      "name": "David",
      "deposits": [
        {"amount": 2000, "entry_spy": 710.6993, "date": "2026-05-09"}
      ]
    },
    {
      "name": "Gabe",
      "deposits": [
        {"amount": 3000, "entry_spy": 710.36, "date": "2026-05-09"}
      ]
    }
  ]
}
```

- [ ] **Step 2: Commit**

```bash
git add investors.json
git commit -m "feat: add initial investors.json with Moses, David, Gabe"
```

---

### Task 2: `app/investors.py` — data model and load/save

**Files:**
- Create: `app/investors.py`
- Create: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_investors.py`:

```python
import json
import pytest


def test_load_investors_returns_empty_list_when_file_missing(tmp_path):
    from app.investors import load_investors
    result = load_investors(path=tmp_path / "missing.json")
    assert result == []


def test_load_investors_parses_name_and_deposits(tmp_path):
    from app.investors import load_investors
    data = {
        "investors": [
            {
                "name": "Moses",
                "deposits": [
                    {"amount": 300.0, "entry_spy": 707.116, "date": "2026-05-09"}
                ],
            }
        ]
    }
    f = tmp_path / "investors.json"
    f.write_text(json.dumps(data))
    result = load_investors(path=f)
    assert len(result) == 1
    assert result[0].name == "Moses"
    assert result[0].deposits[0].amount == 300.0
    assert result[0].deposits[0].entry_spy == 707.116
    assert result[0].deposits[0].date == "2026-05-09"


def test_save_and_reload_roundtrip(tmp_path):
    from app.investors import Deposit, Investor, load_investors, save_investors
    investors = [
        Investor(
            name="Moses",
            deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")],
        )
    ]
    path = tmp_path / "investors.json"
    save_investors(investors, path=path)
    loaded = load_investors(path=path)
    assert loaded[0].name == "Moses"
    assert loaded[0].deposits[0].amount == 300.0
    assert loaded[0].deposits[0].entry_spy == 707.116
    assert loaded[0].deposits[0].date == "2026-05-09"


def test_save_preserves_multiple_investors(tmp_path):
    from app.investors import Deposit, Investor, load_investors, save_investors
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")]),
        Investor(name="David", deposits=[Deposit(amount=2000.0, entry_spy=710.6993, date="2026-05-09")]),
    ]
    path = tmp_path / "investors.json"
    save_investors(investors, path=path)
    loaded = load_investors(path=path)
    assert len(loaded) == 2
    assert loaded[1].name == "David"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_investors.py -v
```

Expected: `ModuleNotFoundError` — `app.investors` does not exist yet.

- [ ] **Step 3: Implement `app/investors.py` — data model and load/save**

Create `app/investors.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

INVESTORS_FILE = Path(__file__).parent.parent / "investors.json"


@dataclass
class Deposit:
    amount: float
    entry_spy: float
    date: str


@dataclass
class Investor:
    name: str
    deposits: list[Deposit] = field(default_factory=list)


def load_investors(path: Path = INVESTORS_FILE) -> list[Investor]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [
        Investor(
            name=inv["name"],
            deposits=[Deposit(**d) for d in inv["deposits"]],
        )
        for inv in data["investors"]
    ]


def save_investors(investors: list[Investor], path: Path = INVESTORS_FILE) -> None:
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
    path.write_text(json.dumps(data, indent=2))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_investors.py -v
```

Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/investors.py tests/test_investors.py
git commit -m "feat: add investors.py with Investor/Deposit dataclasses and load/save"
```

---

### Task 3: `app/investors.py` — equity calculation

**Files:**
- Modify: `app/investors.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investors.py`:

```python
def test_compute_breakdown_single_deposit():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    result = compute_breakdown(investors, spy_price=600.0)
    assert result.investors[0].current_equity == pytest.approx(360.0)
    assert result.investors[0].total_deposited == pytest.approx(300.0)
    assert result.investors[0].dollar_pnl == pytest.approx(60.0)
    assert result.investors[0].pct_pnl == pytest.approx(20.0)
    assert result.investors[0].portfolio_share == pytest.approx(100.0)


def test_compute_breakdown_portfolio_share_splits_evenly():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
        Investor(name="B", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
    ]
    result = compute_breakdown(investors, spy_price=110.0)
    assert result.investors[0].portfolio_share == pytest.approx(50.0)
    assert result.investors[1].portfolio_share == pytest.approx(50.0)


def test_compute_breakdown_multiple_deposits_per_investor():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(
            name="Moses",
            deposits=[
                Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01"),
                Deposit(amount=500.0, entry_spy=600.0, date="2026-06-01"),
            ],
        )
    ]
    # First deposit:  300 * 600/500 = 360.0
    # Second deposit: 500 * 600/600 = 500.0
    result = compute_breakdown(investors, spy_price=600.0)
    assert result.investors[0].current_equity == pytest.approx(860.0)
    assert result.investors[0].total_deposited == pytest.approx(800.0)
    assert result.investors[0].dollar_pnl == pytest.approx(60.0)


def test_compute_breakdown_totals():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
        Investor(name="B", deposits=[Deposit(amount=2000.0, entry_spy=100.0, date="2026-01-01")]),
    ]
    result = compute_breakdown(investors, spy_price=110.0)
    assert result.total_deposited == pytest.approx(3000.0)
    assert result.total_portfolio == pytest.approx(3300.0)
    assert result.overall_dollar_pnl == pytest.approx(300.0)
    assert result.overall_pct_pnl == pytest.approx(10.0)
    assert result.spy_price == 110.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_investors.py::test_compute_breakdown_single_deposit -v
```

Expected: `ImportError: cannot import name 'compute_breakdown'`

- [ ] **Step 3: Add `InvestorResult`, `InvestorBreakdown`, and `compute_breakdown` to `app/investors.py`**

Append to `app/investors.py` (after `save_investors`):

```python
@dataclass
class InvestorResult:
    name: str
    total_deposited: float
    current_equity: float
    dollar_pnl: float
    pct_pnl: float
    portfolio_share: float


@dataclass
class InvestorBreakdown:
    investors: list[InvestorResult]
    spy_price: float
    total_portfolio: float
    total_deposited: float
    overall_dollar_pnl: float
    overall_pct_pnl: float


def compute_breakdown(investors: list[Investor], spy_price: float) -> InvestorBreakdown:
    results: list[InvestorResult] = []
    for inv in investors:
        total_deposited = sum(d.amount for d in inv.deposits)
        current_equity = sum(d.amount * spy_price / d.entry_spy for d in inv.deposits)
        dollar_pnl = current_equity - total_deposited
        pct_pnl = (dollar_pnl / total_deposited * 100) if total_deposited else 0.0
        results.append(
            InvestorResult(
                name=inv.name,
                total_deposited=total_deposited,
                current_equity=current_equity,
                dollar_pnl=dollar_pnl,
                pct_pnl=pct_pnl,
                portfolio_share=0.0,
            )
        )

    total_portfolio = sum(r.current_equity for r in results)
    for r in results:
        r.portfolio_share = (r.current_equity / total_portfolio * 100) if total_portfolio else 0.0

    total_deposited = sum(r.total_deposited for r in results)
    overall_dollar_pnl = total_portfolio - total_deposited
    overall_pct_pnl = (overall_dollar_pnl / total_deposited * 100) if total_deposited else 0.0

    return InvestorBreakdown(
        investors=results,
        spy_price=spy_price,
        total_portfolio=total_portfolio,
        total_deposited=total_deposited,
        overall_dollar_pnl=overall_dollar_pnl,
        overall_pct_pnl=overall_pct_pnl,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/investors.py tests/test_investors.py
git commit -m "feat: add InvestorBreakdown and compute_breakdown to investors.py"
```

---

### Task 4: `app/investors.py` — Discord message formatting

**Files:**
- Modify: `app/investors.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investors.py`:

```python
def test_format_discord_message_contains_investor_name_and_date():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "Moses" in msg
    assert "May 9, 2026" in msg


def test_format_discord_message_shows_current_equity():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "360.00" in msg  # current_equity = 300 * 600/500


def test_format_discord_message_prefixes_positive_pnl_with_plus():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "+$60.00" in msg


def test_format_discord_message_shows_totals():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
        Investor(name="B", deposits=[Deposit(amount=2000.0, entry_spy=100.0, date="2026-01-01")]),
    ]
    breakdown = compute_breakdown(investors, spy_price=110.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "3,300.00" in msg  # total portfolio
    assert "3,000.00" in msg  # total deposited
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_investors.py::test_format_discord_message_contains_investor_name_and_date -v
```

Expected: `ImportError: cannot import name 'format_discord_message'`

- [ ] **Step 3: Add `format_discord_message` to `app/investors.py`**

Append to `app/investors.py`:

```python
def format_discord_message(breakdown: InvestorBreakdown, date_str: str) -> str:
    lines = [
        f"📊 **Investor Breakdown — {date_str}**",
        f"SPY: ${breakdown.spy_price:,.2f}",
        "",
    ]
    for r in breakdown.investors:
        sign = "+" if r.dollar_pnl >= 0 else ""
        lines += [
            f"**{r.name}**",
            f"> Deposited: ${r.total_deposited:,.2f}",
            f"> Current Equity: ${r.current_equity:,.2f}",
            f"> P&L: {sign}${r.dollar_pnl:,.2f} ({sign}{r.pct_pnl:.2f}%)",
            f"> Portfolio Share: {r.portfolio_share:.1f}%",
            "",
        ]
    overall_sign = "+" if breakdown.overall_dollar_pnl >= 0 else ""
    lines += [
        "─" * 25,
        f"**Total Portfolio: ${breakdown.total_portfolio:,.2f}**",
        f"**Total Deposited: ${breakdown.total_deposited:,.2f}**",
        f"**Overall P&L: {overall_sign}${breakdown.overall_dollar_pnl:,.2f} ({overall_sign}{breakdown.overall_pct_pnl:.2f}%)**",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/investors.py tests/test_investors.py
git commit -m "feat: add format_discord_message to investors.py"
```

---

### Task 5: `app/config.py` — add `discord_investors_webhook_url`

**Files:**
- Modify: `app/config.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investors.py`:

```python
def test_config_discord_investors_webhook_url_defaults_to_none():
    from app.config import Settings
    s = Settings(alpaca_api_key="x", alpaca_secret_key="x", webhook_secret="x")
    assert s.discord_investors_webhook_url is None


def test_config_accepts_discord_investors_webhook_url():
    from app.config import Settings
    s = Settings(
        alpaca_api_key="x",
        alpaca_secret_key="x",
        webhook_secret="x",
        discord_investors_webhook_url="https://discord.com/api/webhooks/999/abc",
    )
    assert s.discord_investors_webhook_url == "https://discord.com/api/webhooks/999/abc"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_investors.py::test_config_discord_investors_webhook_url_defaults_to_none -v
```

Expected: `ValidationError` or `AttributeError` — field does not exist on `Settings`.

- [ ] **Step 3: Add the field to `app/config.py`**

Inside the `Settings` class in `app/config.py`, add after the existing `discord_webhook_url` field:

```python
discord_investors_webhook_url: Optional[str] = None
```

If `Optional` is not already imported, add `from typing import Optional` at the top of the file.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_investors.py
git commit -m "feat: add optional discord_investors_webhook_url to Settings"
```

---

### Task 6: `app/notifications.py` — add `notify_investors()`

**Files:**
- Modify: `app/notifications.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investors.py`:

```python
import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_notify_investors_posts_to_investors_webhook():
    from unittest.mock import AsyncMock, MagicMock, patch
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=MagicMock())
    with patch("app.notifications.settings") as mock_settings:
        mock_settings.discord_investors_webhook_url = "https://discord.com/investors"
        mock_settings.discord_webhook_url = "https://discord.com/main"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.notifications import notify_investors
            await notify_investors("test message")
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://discord.com/investors"


@pytest.mark.asyncio
async def test_notify_investors_falls_back_to_main_webhook():
    from unittest.mock import AsyncMock, MagicMock, patch
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=MagicMock())
    with patch("app.notifications.settings") as mock_settings:
        mock_settings.discord_investors_webhook_url = None
        mock_settings.discord_webhook_url = "https://discord.com/main"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.notifications import notify_investors
            await notify_investors("test message")
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://discord.com/main"


@pytest.mark.asyncio
async def test_notify_investors_skips_when_no_webhooks_set():
    from unittest.mock import AsyncMock, patch
    with patch("app.notifications.settings") as mock_settings:
        mock_settings.discord_investors_webhook_url = None
        mock_settings.discord_webhook_url = None
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.notifications import notify_investors
            await notify_investors("test message")
    mock_client.post.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_investors.py::test_notify_investors_posts_to_investors_webhook -v
```

Expected: `ImportError: cannot import name 'notify_investors'`

- [ ] **Step 3: Add `notify_investors` to `app/notifications.py`**

Append to `app/notifications.py` after the existing `notify()` function:

```python
async def notify_investors(message: str) -> None:
    url = settings.discord_investors_webhook_url or settings.discord_webhook_url
    if not url:
        logger.warning("No Discord webhook configured for investor notifications; skipping")
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        logger.warning("Investor Discord notification failed: %s", exc)
```

`httpx`, `logger`, and `settings` are already imported in `notifications.py`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/notifications.py tests/test_investors.py
git commit -m "feat: add notify_investors() with fallback to main Discord channel"
```

---

### Task 7: `app/models.py` — add `DepositRequest`

**Files:**
- Modify: `app/models.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investors.py`:

```python
def test_deposit_request_valid_without_spy_price():
    from app.models import DepositRequest
    req = DepositRequest(secret="s", investor="Moses", amount=500.0)
    assert req.spy_price is None
    assert req.amount == 500.0
    assert req.investor == "Moses"


def test_deposit_request_valid_with_explicit_spy_price():
    from app.models import DepositRequest
    req = DepositRequest(secret="s", investor="Moses", amount=500.0, spy_price=580.0)
    assert req.spy_price == 580.0


def test_deposit_request_rejects_zero_amount():
    from pydantic import ValidationError
    from app.models import DepositRequest
    with pytest.raises(ValidationError):
        DepositRequest(secret="s", investor="Moses", amount=0.0)


def test_deposit_request_rejects_negative_amount():
    from pydantic import ValidationError
    from app.models import DepositRequest
    with pytest.raises(ValidationError):
        DepositRequest(secret="s", investor="Moses", amount=-100.0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_investors.py::test_deposit_request_valid_without_spy_price -v
```

Expected: `ImportError: cannot import name 'DepositRequest'`

- [ ] **Step 3: Add `DepositRequest` to `app/models.py`**

Append to `app/models.py` after the existing models:

```python
class DepositRequest(BaseModel):
    secret: str
    investor: str
    amount: float
    spy_price: Optional[float] = None

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v
```

`BaseModel` and `field_validator` are already used in `models.py`. If `Optional` is not already imported, add `from typing import Optional` at the top of the file.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_investors.py
git commit -m "feat: add DepositRequest Pydantic model with positive-amount validation"
```

---

### Task 8: `app/pnl.py` — add `send_investor_report()`

**Files:**
- Modify: `app/pnl.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investors.py`:

```python
@pytest.mark.asyncio
async def test_send_investor_report_skips_when_no_investors():
    from unittest.mock import AsyncMock, patch
    with patch("app.pnl.load_investors", return_value=[]):
        with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
            from app.pnl import send_investor_report
            await send_investor_report()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_send_investor_report_skips_when_spy_price_unavailable():
    from unittest.mock import AsyncMock, patch
    from app.investors import Deposit, Investor
    mock_investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    with patch("app.pnl.load_investors", return_value=mock_investors):
        with patch("app.pnl.get_latest_price", return_value=None):
            with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
                from app.pnl import send_investor_report
                await send_investor_report()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_send_investor_report_sends_message_with_investor_name():
    from unittest.mock import AsyncMock, patch
    from app.investors import Deposit, Investor
    mock_investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    with patch("app.pnl.load_investors", return_value=mock_investors):
        with patch("app.pnl.get_latest_price", return_value=600.0):
            with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
                from app.pnl import send_investor_report
                await send_investor_report()
    mock_notify.assert_called_once()
    message = mock_notify.call_args[0][0]
    assert "Moses" in message
    assert "360.00" in message  # 300 * 600/500
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_investors.py::test_send_investor_report_skips_when_no_investors -v
```

Expected: `ImportError: cannot import name 'send_investor_report'`

- [ ] **Step 3: Add imports and `send_investor_report` to `app/pnl.py`**

Add these imports to the top of `app/pnl.py` alongside existing imports:

```python
from datetime import datetime

from app.investors import compute_breakdown, format_discord_message, load_investors
from app.notifications import notify_investors
from app.trading.alpaca_client import get_latest_price
```

Then append to `app/pnl.py`:

```python
async def send_investor_report() -> None:
    investors = load_investors()
    if not investors:
        logger.warning("No investors found; skipping investor report")
        return

    spy_price = get_latest_price("SPY")
    if spy_price is None:
        logger.warning("Could not fetch SPY price; skipping investor report")
        return

    now = datetime.now(ET)
    date_str = now.strftime(f"%B {now.day}, %Y")
    breakdown = compute_breakdown(investors, spy_price)
    message = format_discord_message(breakdown, date_str)
    await notify_investors(message)
    logger.info("Investor report sent for %s", date_str)
```

`logger` and `ET` (the Eastern Time timezone object) are already defined in `pnl.py`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/pnl.py tests/test_investors.py
git commit -m "feat: add send_investor_report() to pnl.py"
```

---

### Task 9: `app/scheduler.py` — register investor breakdown cron jobs

**Files:**
- Modify: `app/scheduler.py`
- Modify: `tests/test_investors.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_investors.py`:

```python
def test_setup_jobs_registers_investor_breakdown_jobs():
    from unittest.mock import MagicMock, patch
    mock_scheduler = MagicMock()
    with patch("app.scheduler.scheduler", mock_scheduler):
        from app.scheduler import setup_jobs
        setup_jobs()
    registered_ids = [call.kwargs.get("id") for call in mock_scheduler.add_job.call_args_list]
    assert "investor_breakdown_daily" in registered_ids
    assert "investor_breakdown_weekly" in registered_ids
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_investors.py::test_setup_jobs_registers_investor_breakdown_jobs -v
```

Expected: `AssertionError` — neither job ID is registered yet.

- [ ] **Step 3: Update `app/scheduler.py`**

Update the import from `app.pnl` to include `send_investor_report`:

```python
from app.pnl import send_daily_report, send_investor_report, send_weekly_report
```

Inside `setup_jobs()`, after the existing two `add_job` calls, append:

```python
scheduler.add_job(
    send_investor_report,
    CronTrigger(day_of_week="mon-thu", hour=16, minute=2, timezone=ET),
    id="investor_breakdown_daily",
    replace_existing=True,
)
scheduler.add_job(
    send_investor_report,
    CronTrigger(day_of_week="fri", hour=16, minute=3, timezone=ET),
    id="investor_breakdown_weekly",
    replace_existing=True,
)
```

`CronTrigger` and `ET` are already imported and defined in `scheduler.py`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_investors.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_investors.py
git commit -m "feat: schedule investor breakdown at 4:02 ET (daily) and 4:03 ET (Friday)"
```

---

### Task 10: `app/main.py` — `POST /deposit` endpoint

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_deposit.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deposit.py`:

```python
import os

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

from unittest.mock import patch
from fastapi.testclient import TestClient

from app.investors import Deposit, Investor
from app.main import app

client = TestClient(app)
TEST_SECRET = "test-secret"


def _initial_investors():
    return [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=707.116, date="2026-05-09")])
    ]


def test_deposit_rejects_wrong_secret():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        response = client.post("/deposit", json={
            "secret": "wrong-secret",
            "investor": "Moses",
            "amount": 500.0,
        })
    assert response.status_code == 401


def test_deposit_appends_to_existing_investor():
    with patch("app.main.load_investors", return_value=_initial_investors()):
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_deposit.py -v
```

Expected: all fail with 404 — `/deposit` route does not exist yet.

- [ ] **Step 3: Add imports and `/deposit` endpoint to `app/main.py`**

Add to the imports at the top of `app/main.py`:

```python
from datetime import date

from app.investors import Deposit, Investor, load_investors, save_investors
from app.models import DepositRequest
from app.trading.alpaca_client import get_latest_price
```

Add the endpoint after the existing `/webhook` handler:

```python
@app.post("/deposit")
async def deposit(request: Request) -> dict:
    body = await request.json()
    req = DepositRequest(**body)
    verify_webhook_secret(req.secret)

    investors = load_investors()
    match = next(
        (inv for inv in investors if inv.name.lower() == req.investor.lower()),
        None,
    )

    spy_price = req.spy_price
    if spy_price is None:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            raise HTTPException(status_code=502, detail="Could not fetch current SPY price from Alpaca.")

    new_deposit = Deposit(amount=req.amount, entry_spy=spy_price, date=date.today().isoformat())

    if match is None:
        match = Investor(name=req.investor, deposits=[new_deposit])
        investors.append(match)
    else:
        match.deposits.append(new_deposit)

    save_investors(investors)

    return {
        "investor": match.name,
        "deposits": [
            {"amount": d.amount, "entry_spy": d.entry_spy, "date": d.date}
            for d in match.deposits
        ],
    }
```

`verify_webhook_secret` is already imported from `app.security`. `HTTPException` and `Request` are already imported from `fastapi`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_deposit.py -v
```

Expected: all 7 tests PASSED.

- [ ] **Step 5: Run the full test suite**

```bash
pytest -v
```

Expected: all tests PASSED.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_deposit.py
git commit -m "feat: add POST /deposit endpoint for recording investor deposits and new members"
```

---

## Done

All tasks complete. The investor breakdown Discord message fires automatically each trading day at 4:02 PM ET (Mon–Thu) and 4:03 PM ET (Friday).

To record a deposit via curl once deployed to Render:

```bash
curl -X POST https://your-render-url.onrender.com/deposit \
  -H "Content-Type: application/json" \
  -d '{"secret": "YOUR_SECRET", "investor": "Moses", "amount": 500, "spy_price": null}'
```

After each deposit via the endpoint, commit the updated `investors.json` to git so the state survives Render redeploys.

To add `DISCORD_INVESTORS_WEBHOOK_URL` in Render: go to your service → Environment → add the variable. If left unset, the breakdown posts to the main Discord channel instead.
