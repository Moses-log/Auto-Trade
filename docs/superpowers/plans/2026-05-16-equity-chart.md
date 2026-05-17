# Portfolio Equity Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach a portfolio vs SPY % return PNG chart to weekly, monthly, yearly, and all-time P&L Discord reports.

**Architecture:** New `app/chart.py` generates a matplotlib PNG in memory. New `notify_with_chart` in `notifications.py` posts it to Discord as a multipart file attachment. Four existing report functions in `pnl.py` call both after computing the text report — falling back to text-only if chart generation fails.

**Tech Stack:** matplotlib, httpx multipart, yfinance, existing Alpaca portfolio history.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `requirements.txt` | Modify | Add matplotlib |
| `app/chart.py` | Create | `generate_equity_chart` — PNG generation |
| `app/pnl.py` | Modify | Add `fetch_spy_history`; update 4 report functions |
| `app/notifications.py` | Modify | Add `notify_with_chart` |
| `tests/test_chart.py` | Create | Chart generation tests |
| `tests/test_pnl.py` | Modify | Update `FakeHistory` + 4 report tests |

---

## Task 1: Add matplotlib to requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add matplotlib**

In `requirements.txt`, add after the `tenacity` / `cryptography` block:

```
# Chart generation for P&L reports
matplotlib>=3.8.0
```

- [ ] **Step 2: Install**

```
py -m pip install "matplotlib>=3.8.0"
```
Expected: installs successfully.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore(deps): add matplotlib for equity chart generation"
```

---

## Task 2: Create `app/chart.py` with tests

**Files:**
- Create: `app/chart.py`
- Create: `tests/test_chart.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chart.py`:

```python
import os
import time
import pytest
import pandas as pd

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

_PNG_MAGIC = b"\x89PNG"


def _fake_spy_df():
    dates = pd.date_range(start="2026-05-09", periods=4, freq="D")
    return pd.DataFrame({"Close": [530.0, 533.0, 536.0, 538.0]}, index=dates)


def _fake_timestamps(n=4):
    now = int(time.time())
    return [now - 86400 * (n - 1 - i) for i in range(n)]


def test_generate_equity_chart_returns_png_bytes():
    from app.chart import generate_equity_chart
    equity = [10000.0, 10100.0, 10250.0, 10320.0]
    timestamps = _fake_timestamps(4)
    result = generate_equity_chart(equity, timestamps, _fake_spy_df(), "Test Chart")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC


def test_generate_equity_chart_handles_none_spy():
    from app.chart import generate_equity_chart
    equity = [10000.0, 10500.0]
    timestamps = _fake_timestamps(2)
    result = generate_equity_chart(equity, timestamps, None, "No SPY Chart")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC


def test_generate_equity_chart_handles_empty_spy_df():
    from app.chart import generate_equity_chart
    equity = [10000.0, 10200.0]
    timestamps = _fake_timestamps(2)
    result = generate_equity_chart(equity, timestamps, pd.DataFrame(), "Empty SPY")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC


def test_generate_equity_chart_single_data_point():
    from app.chart import generate_equity_chart
    equity = [10000.0]
    timestamps = [int(time.time())]
    result = generate_equity_chart(equity, timestamps, None, "Single Point")
    assert isinstance(result, bytes)
    assert result[:4] == _PNG_MAGIC
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_chart.py -v
```
Expected: `ImportError` — `app.chart` not found.

- [ ] **Step 3: Create `app/chart.py`**

```python
from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — required for server use
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pytz

ET = pytz.timezone("America/New_York")


def generate_equity_chart(
    equity: list,
    timestamps: list,
    spy_df,
    title: str,
) -> bytes:
    """Generate a portfolio vs SPY % return chart as PNG bytes.

    Both series are normalized to % return from the start of the period
    so they share a 0% baseline regardless of dollar amounts.

    Args:
        equity:     Portfolio equity values from Alpaca history.
        timestamps: Corresponding Unix timestamps from Alpaca history.
        spy_df:     yfinance DataFrame with a "Close" column, or None.
        title:      Chart title string.

    Returns:
        PNG image as bytes.
    """
    dates = [datetime.fromtimestamp(ts, tz=ET).date() for ts in timestamps]

    open_eq = equity[0] if equity and equity[0] else 1.0
    port_pct = [(eq - open_eq) / open_eq * 100 for eq in equity]

    spy_pct: list = []
    spy_dates: list = []
    if spy_df is not None and not spy_df.empty and "Close" in spy_df.columns:
        spy_open = float(spy_df["Close"].iloc[0])
        if spy_open:
            spy_pct = [(float(p) - spy_open) / spy_open * 100 for p in spy_df["Close"]]
            spy_dates = [
                d.date() if hasattr(d, "date") else d for d in spy_df.index
            ]

    fig, ax = plt.subplots(figsize=(10, 5))

    ax.axhline(0, color="grey", linestyle="--", linewidth=0.8, alpha=0.5)

    final_port = port_pct[-1] if port_pct else 0.0
    port_sign = "+" if final_port >= 0 else ""
    ax.plot(
        dates, port_pct,
        color="#2ecc71", linewidth=2,
        label=f"Portfolio {port_sign}{final_port:.2f}%",
    )

    if spy_pct and spy_dates:
        final_spy = spy_pct[-1]
        spy_sign = "+" if final_spy >= 0 else ""
        ax.plot(
            spy_dates, spy_pct,
            color="#e67e22", linestyle="--", linewidth=1.5,
            label=f"S&P 500 {spy_sign}{final_spy:.2f}%",
        )

    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_ylabel("Return (%)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:+.1f}%"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.xticks(rotation=30, ha="right")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
```

- [ ] **Step 4: Run tests to verify they pass**

```
py -m pytest tests/test_chart.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/chart.py tests/test_chart.py
git commit -m "feat(chart): add equity chart generator"
```

---

## Task 3: Add `fetch_spy_history` to `app/pnl.py`

**Files:**
- Modify: `app/pnl.py`
- Modify: `tests/test_pnl.py`

**Context:** `pnl.py` already imports `yf` (yfinance). `compute_spy_pct` returns a single float. `fetch_spy_history` returns the full DataFrame needed by the chart generator. `send_alltime_report` already has an inline yfinance call that will be replaced by this helper in Task 5.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pnl.py` after the existing SPY tests:

```python
# ── fetch_spy_history tests ───────────────────────────────────────────────────

def test_fetch_spy_history_returns_dataframe():
    from datetime import date
    import pandas as pd
    fake_df = pd.DataFrame(
        {"Close": [537.0, 539.0]},
        index=pd.date_range("2026-05-09", periods=2),
    )
    with patch("app.pnl.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.history.return_value = fake_df
        from app.pnl import fetch_spy_history
        result = fetch_spy_history(date(2026, 5, 9), date(2026, 5, 16))
    assert result is not None
    assert "Close" in result.columns


def test_fetch_spy_history_returns_none_on_empty():
    from datetime import date
    import pandas as pd
    with patch("app.pnl.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.history.return_value = pd.DataFrame()
        from app.pnl import fetch_spy_history
        result = fetch_spy_history(date(2026, 5, 9), date(2026, 5, 16))
    assert result is None


def test_fetch_spy_history_returns_none_on_exception():
    from datetime import date
    with patch("app.pnl.yf.Ticker", side_effect=Exception("network error")):
        from app.pnl import fetch_spy_history
        result = fetch_spy_history(date(2026, 5, 9), date(2026, 5, 16))
    assert result is None
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_pnl.py::test_fetch_spy_history_returns_dataframe -v
```
Expected: `AttributeError` — `fetch_spy_history` not defined.

- [ ] **Step 3: Add `fetch_spy_history` and new imports to `app/pnl.py`**

Add to the imports at the top of `app/pnl.py` (after existing imports):

```python
from datetime import date as _date
```

Add the function after `compute_spy_pct`:

```python
def fetch_spy_history(start_date: _date, end_date: _date):
    """Fetch SPY price history between two dates for chart generation.

    Returns a yfinance DataFrame with a "Close" column, or None on failure.
    """
    try:
        hist = yf.Ticker("SPY").history(start=start_date, end=end_date)
        if hist.empty:
            return None
        return hist
    except Exception as exc:
        log.warning("yfinance SPY history fetch failed: %s", exc)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```
py -m pytest tests/test_pnl.py::test_fetch_spy_history_returns_dataframe tests/test_pnl.py::test_fetch_spy_history_returns_none_on_empty tests/test_pnl.py::test_fetch_spy_history_returns_none_on_exception -v
```
Expected: 3 passed.

- [ ] **Step 5: Run full pnl test suite**

```
py -m pytest tests/test_pnl.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/pnl.py tests/test_pnl.py
git commit -m "feat(pnl): add fetch_spy_history helper for chart generation"
```

---

## Task 4: Add `notify_with_chart` to `app/notifications.py`

**Files:**
- Modify: `app/notifications.py`
- Modify: `tests/test_investors.py` (notification tests live here)

**Context:** `notifications.py` uses `httpx.AsyncClient` for all Discord POSTs. The existing `notify()` posts JSON. `notify_with_chart` posts multipart/form-data — `payload_json` field for the message text, `file` field for the PNG. Same webhook URL as `notify()`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_investors.py` (where other notification tests live):

```python
@pytest.mark.asyncio
async def test_notify_with_chart_posts_multipart_to_webhook():
    from unittest.mock import AsyncMock, patch, MagicMock
    with patch("app.notifications.settings") as mock_settings, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_settings.discord_webhook_url = "https://discord.com/api/webhooks/test"
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        from app.notifications import notify_with_chart
        await notify_with_chart("P&L message", b"fake_png")
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args[1]
    assert "files" in call_kwargs
    assert "data" in call_kwargs


@pytest.mark.asyncio
async def test_notify_with_chart_skips_when_no_webhook():
    from unittest.mock import patch, AsyncMock
    with patch("app.notifications.settings") as mock_settings:
        mock_settings.discord_webhook_url = None
        with patch("httpx.AsyncClient") as mock_cls:
            from app.notifications import notify_with_chart
            await notify_with_chart("msg", b"bytes")
    mock_cls.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_investors.py::test_notify_with_chart_posts_multipart_to_webhook -v
```
Expected: `ImportError` — `notify_with_chart` not defined.

- [ ] **Step 3: Add `notify_with_chart` to `app/notifications.py`**

Add after `notify_investors`:

```python
async def notify_with_chart(message: str, chart_bytes: bytes) -> None:
    """Send a Discord message with a PNG chart attachment to the main channel."""
    import json as _json
    url = settings.discord_webhook_url
    if not url:
        log.warning("DISCORD_WEBHOOK_URL not set; skipping chart notification")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                url,
                data={"payload_json": _json.dumps({"content": message[:2000]})},
                files={"file": ("chart.png", chart_bytes, "image/png")},
            )
    except Exception as exc:
        log.warning("Discord chart notification failed: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

```
py -m pytest tests/test_investors.py::test_notify_with_chart_posts_multipart_to_webhook tests/test_investors.py::test_notify_with_chart_skips_when_no_webhook -v
```
Expected: 2 passed.

- [ ] **Step 5: Run full investor test suite**

```
py -m pytest tests/test_investors.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/notifications.py tests/test_investors.py
git commit -m "feat(notifications): add notify_with_chart for Discord image attachments"
```

---

## Task 5: Update 4 report functions and tests in `app/pnl.py`

**Files:**
- Modify: `app/pnl.py`
- Modify: `tests/test_pnl.py`

**Context:** `send_weekly_report`, `send_monthly_report`, `send_yearly_report`, `send_alltime_report` all call `await notify(msg)`. Each will now attempt chart generation and call `notify_with_chart` instead, falling back to `notify` on any chart failure.

`FakeHistory` in `test_pnl.py` only has `equity`. The report functions now access `history.timestamp`, so `FakeHistory` needs a `timestamp` field. Update it with a default that auto-generates timestamps.

The start date for `fetch_spy_history` comes from `history.timestamp[0]` converted to a date — this ensures the SPY series covers the same period as the Alpaca data.

- [ ] **Step 1: Update `FakeHistory` and add chart mocks to existing tests**

In `tests/test_pnl.py`, update `FakeHistory`:

```python
import time as _time
from dataclasses import dataclass, field


@dataclass
class FakeHistory:
    equity: list
    timestamp: list = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            now = int(_time.time())
            n = len(self.equity)
            self.timestamp = [now - 86400 * (n - 1 - i) for i in range(n)]
```

Update the 4 existing report success tests to mock `generate_equity_chart` and `notify_with_chart`. Replace `test_send_weekly_report_success`, `test_send_monthly_report_success`, `test_send_yearly_report_success`, `test_send_alltime_report_success`:

```python
@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.fetch_spy_history", return_value=None)
@patch("app.pnl.generate_equity_chart", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
@patch("app.pnl.notify_with_chart", new_callable=AsyncMock)
async def test_send_weekly_report_success(mock_chart_notify, mock_notify, mock_get_history, mock_chart, mock_spy_hist, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10875.20])
    from app.pnl import send_weekly_report
    await send_weekly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Weekly P&L" in msg
    assert "$10,875.20" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.fetch_spy_history", return_value=None)
@patch("app.pnl.generate_equity_chart", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
@patch("app.pnl.notify_with_chart", new_callable=AsyncMock)
async def test_send_monthly_report_success(mock_chart_notify, mock_notify, mock_get_history, mock_chart, mock_spy_hist, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10500.0])
    from app.pnl import send_monthly_report
    await send_monthly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Monthly P&L" in msg
    assert "$10,500.00" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.fetch_spy_history", return_value=None)
@patch("app.pnl.generate_equity_chart", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
@patch("app.pnl.notify_with_chart", new_callable=AsyncMock)
async def test_send_yearly_report_success(mock_chart_notify, mock_notify, mock_get_history, mock_chart, mock_spy_hist, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 12000.0])
    from app.pnl import send_yearly_report
    await send_yearly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Yearly P&L" in msg
    assert "$12,000.00" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.fetch_spy_history", return_value=None)
@patch("app.pnl.generate_equity_chart", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
@patch("app.pnl.notify_with_chart", new_callable=AsyncMock)
async def test_send_alltime_report_success(mock_chart_notify, mock_notify, mock_get_history, mock_chart, mock_spy_hist, mock_spy):
    import time
    fake_history = MagicMock()
    fake_history.equity = [0.0, 10000.0, 11500.0]
    fake_history.timestamp = [
        int(time.time()) - 86400 * 10,
        int(time.time()) - 86400 * 5,
        int(time.time()),
    ]
    mock_get_history.return_value = fake_history
    from app.pnl import send_alltime_report
    await send_alltime_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "All-Time P&L" in msg
    assert "since" in msg
```

Also add one test verifying chart is sent when chart generation succeeds:

```python
@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=1.2)
@patch("app.pnl.fetch_spy_history", return_value=MagicMock())
@patch("app.pnl.generate_equity_chart", return_value=b"\x89PNG_fake")
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
@patch("app.pnl.notify_with_chart", new_callable=AsyncMock)
async def test_send_weekly_report_sends_chart_when_available(mock_chart_notify, mock_notify, mock_get_history, mock_chart, mock_spy_hist, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10875.20])
    from app.pnl import send_weekly_report
    await send_weekly_report()
    mock_chart_notify.assert_called_once()
    mock_notify.assert_not_called()
```

- [ ] **Step 2: Run to verify test failures (before implementation)**

```
py -m pytest tests/test_pnl.py::test_send_weekly_report_success -v
```
Expected: FAIL — `app.pnl` has no attribute `generate_equity_chart` or `notify_with_chart`.

- [ ] **Step 3: Add imports to `app/pnl.py`**

Add after existing imports in `app/pnl.py`:

```python
from app.chart import generate_equity_chart
from app.notifications import notify, notify_investors, notify_with_chart
```

Note: `notify` and `notify_investors` are already imported — just add `notify_with_chart` to the existing import line.

The full updated import line:
```python
from app.notifications import notify, notify_investors, notify_with_chart
```

And add chart import separately:
```python
from app.chart import generate_equity_chart
```

- [ ] **Step 4: Update `send_weekly_report` in `app/pnl.py`**

Replace the existing `send_weekly_report` function:

```python
async def send_weekly_report() -> None:
    """Fetch weekly portfolio history and post P&L + chart to Discord."""
    now = datetime.now(ET)
    monday = now - timedelta(days=now.weekday())
    date_str = f"Week of {monday.strftime('%b')} {monday.day}–{now.day}, {now.year}"
    chart_title = f"Weekly Performance: {monday.strftime('%b %d')}–{now.strftime('%b %d, %Y')}"
    try:
        history = get_portfolio_history(period="1W", timeframe="1D")
        result = _compute_pnl(history, "weekly")
        spy_pct = compute_spy_pct("5d")
        msg = _format_message(result, "Weekly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            start_date = datetime.fromtimestamp(history.timestamp[0], tz=ET).date()
            end_date = now.date() + timedelta(days=1)
            spy_df = fetch_spy_history(start_date, end_date)
            if spy_df is not None:
                chart_bytes = generate_equity_chart(
                    history.equity, history.timestamp, spy_df, chart_title
                )
        except Exception as exc:
            log.warning("Weekly chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("Weekly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Weekly P&L report failed: %s", exc)
        await notify(f"⚠️ Weekly P&L report failed: {exc}")
```

- [ ] **Step 5: Update `send_monthly_report` in `app/pnl.py`**

Replace the existing `send_monthly_report` function:

```python
async def send_monthly_report() -> None:
    """Fetch monthly portfolio history and post P&L + chart to Discord."""
    now = datetime.now(ET)
    date_str = f"Month of {now.strftime('%B %Y')}"
    chart_title = f"Monthly Performance: {now.strftime('%B %Y')}"
    try:
        history = get_portfolio_history(period="1M", timeframe="1D")
        result = _compute_pnl(history, "monthly")
        spy_pct = compute_spy_pct("1mo")
        msg = _format_message(result, "Monthly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            start_date = datetime.fromtimestamp(history.timestamp[0], tz=ET).date()
            end_date = now.date() + timedelta(days=1)
            spy_df = fetch_spy_history(start_date, end_date)
            if spy_df is not None:
                chart_bytes = generate_equity_chart(
                    history.equity, history.timestamp, spy_df, chart_title
                )
        except Exception as exc:
            log.warning("Monthly chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("Monthly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Monthly P&L report failed: %s", exc)
        await notify(f"⚠️ Monthly P&L report failed: {exc}")
```

- [ ] **Step 6: Update `send_yearly_report` in `app/pnl.py`**

Replace the existing `send_yearly_report` function:

```python
async def send_yearly_report() -> None:
    """Fetch trailing 12-month (1 year) portfolio history and post P&L + chart to Discord."""
    now = datetime.now(ET)
    date_str = f"Year {now.year}"
    chart_title = f"1-Year Performance through {now.strftime('%b %d, %Y')}"
    try:
        history = get_portfolio_history(period="1A", timeframe="1D")
        result = _compute_pnl(history, "yearly")
        spy_pct = compute_spy_pct("1y")
        msg = _format_message(result, "Yearly P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            start_date = datetime.fromtimestamp(history.timestamp[0], tz=ET).date()
            end_date = now.date() + timedelta(days=1)
            spy_df = fetch_spy_history(start_date, end_date)
            if spy_df is not None:
                chart_bytes = generate_equity_chart(
                    history.equity, history.timestamp, spy_df, chart_title
                )
        except Exception as exc:
            log.warning("Yearly chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("Yearly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Yearly P&L report failed: %s", exc)
        await notify(f"⚠️ Yearly P&L report failed: {exc}")
```

- [ ] **Step 7: Update `send_alltime_report` in `app/pnl.py`**

Replace the existing `send_alltime_report` function (note: this refactors the inline yfinance call into `fetch_spy_history`):

```python
async def send_alltime_report() -> None:
    """Fetch all-time portfolio history and post P&L + chart to Discord."""
    now = datetime.now(ET)
    try:
        history = get_portfolio_history(period="all", timeframe="1D")

        # Find first non-zero equity to skip pre-portfolio zeros
        start_idx = next(
            (i for i, eq in enumerate(history.equity) if eq and eq > 0),
            0,
        )
        open_eq = history.equity[start_idx]
        close_eq = history.equity[-1]
        dollar = close_eq - open_eq
        pct = (dollar / open_eq * 100) if open_eq else 0.0
        result = PnLResult(period="alltime", close_equity=close_eq, dollar_pnl=dollar, pct_pnl=pct)

        start_dt = None
        start_str = "inception"
        if start_idx < len(history.timestamp):
            start_dt = datetime.fromtimestamp(history.timestamp[start_idx], tz=ET)
            start_str = start_dt.strftime(f"%b {start_dt.day}, %Y")
        date_str = f"All Time since {start_str}"
        chart_title = f"All-Time Performance since {start_str}"

        # Fetch SPY over same date range as portfolio
        spy_pct: Optional[float] = None
        spy_df = None
        if start_dt is not None:
            spy_df = fetch_spy_history(start_dt.date(), now.date() + timedelta(days=1))
            if spy_df is not None and not spy_df.empty:
                try:
                    spy_open = float(spy_df["Open"].iloc[0])
                    spy_close = float(spy_df["Close"].iloc[-1])
                    if spy_open:
                        spy_pct = (spy_close - spy_open) / spy_open * 100
                except Exception as exc:
                    log.warning("SPY all-time pct calc failed: %s", exc)

        msg = _format_message(result, "All-Time P&L", date_str, spy_pct=spy_pct)

        chart_bytes = None
        try:
            if spy_df is not None:
                chart_bytes = generate_equity_chart(
                    history.equity[start_idx:], history.timestamp[start_idx:],
                    spy_df, chart_title
                )
        except Exception as exc:
            log.warning("All-time chart generation failed: %s", exc)

        if chart_bytes:
            await notify_with_chart(msg, chart_bytes)
        else:
            await notify(msg)
        log.info("All-time P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("All-time P&L report failed: %s", exc)
        await notify(f"⚠️ All-time P&L report failed: {exc}")
```

- [ ] **Step 8: Run full test suite**

```
py -m pytest tests/ -v
```
Expected: all pass (1 pre-existing failure in `test_github_commit.py::test_commit_raises_on_get_failure` — ignore).

- [ ] **Step 9: Commit and push**

```bash
git add app/pnl.py tests/test_pnl.py
git commit -m "feat(pnl): attach equity chart to weekly, monthly, yearly, all-time reports"
git push
```

---

## Self-Review

**Spec coverage:**
- ✅ `generate_equity_chart` returns PNG bytes — Task 2
- ✅ Both lines normalized to % return from period start — Task 2
- ✅ Portfolio green solid, SPY orange dashed — Task 2
- ✅ `fetch_spy_history(start_date, end_date)` helper — Task 3
- ✅ `notify_with_chart` posts multipart to Discord — Task 4
- ✅ Weekly report gets chart — Task 5
- ✅ Monthly report gets chart — Task 5
- ✅ Yearly report gets chart — Task 5
- ✅ All-time report gets chart — Task 5
- ✅ Chart failures fall back to text-only — Task 5 (try/except around chart block)
- ✅ Nothing written to disk (BytesIO) — Task 2
- ✅ matplotlib Agg backend for server use — Task 2
- ✅ matplotlib added to requirements.txt — Task 1

**Type consistency:**
- `generate_equity_chart(equity: list, timestamps: list, spy_df, title: str) -> bytes` — consistent Tasks 2 & 5
- `fetch_spy_history(start_date: _date, end_date: _date)` — consistent Tasks 3 & 5
- `notify_with_chart(message: str, chart_bytes: bytes) -> None` — consistent Tasks 4 & 5
- `FakeHistory.timestamp` — added in Task 5, used in Task 5 report tests ✅
