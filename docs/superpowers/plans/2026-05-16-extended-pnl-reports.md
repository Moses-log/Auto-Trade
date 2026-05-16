# Extended P&L Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add monthly, yearly, YTD, and all-time P&L reports — auto-scheduled and on-demand via Discord slash commands.

**Architecture:** Four new async report functions in `app/pnl.py` follow the exact same pattern as `send_daily_report` and `send_weekly_report`. A new `check_period_reports` function in `pnl.py` fires Mon–Fri at 4:05 PM ET via APScheduler and conditionally sends monthly/yearly reports on the last trading day of each period. Discord and `/run-report` routing updated to expose all four new types.

**Tech Stack:** Alpaca portfolio history API, yfinance, APScheduler, existing pnl/scheduler/discord patterns.

---

## File Map

| File | Action | Change |
|---|---|---|
| `app/pnl.py` | Modify | Add `send_monthly_report`, `send_yearly_report`, `send_ytd_report`, `send_alltime_report`, `check_period_reports` |
| `app/scheduler.py` | Modify | Register `period_pnl_check` job in `setup_jobs()` |
| `app/discord_commands.py` | Modify | Route 4 new types in `handle_report`, add imports |
| `app/main.py` | Modify | Expand `/run-report` allowlist and routing, add imports |
| `scripts/register_commands.py` | Modify | Add 4 new choices to `/report` command |
| `tests/test_pnl.py` | Modify | Add tests for 4 new report functions + `check_period_reports` |
| `tests/test_discord_commands.py` | Modify | Add routing test for new report types |

---

## Task 1: Add 4 new report functions + `check_period_reports` to `app/pnl.py`

**Files:**
- Modify: `app/pnl.py`
- Modify: `tests/test_pnl.py`

**Context:** `app/pnl.py` currently has `send_daily_report` (period `"1D"`, timeframe `"1Min"`, SPY `"1d"`) and `send_weekly_report` (period `"1W"`, timeframe `"1D"`, SPY `"5d"`). New functions follow identical structure. `_compute_pnl`, `_format_message`, `compute_spy_pct`, `notify`, and `get_portfolio_history` are all reused without modification. `PnLResult` dataclass is also reused.

`get_next_trading_day` is already in `app/trading/alpaca_client.py` — import it at the top of `pnl.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pnl.py` (after the existing tests):

```python
# ── New extended report tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_monthly_report_success(mock_notify, mock_get_history, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10500.0])
    from app.pnl import send_monthly_report
    await send_monthly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Monthly P&L" in msg
    assert "$10,500.00" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_monthly_report_alpaca_error(mock_notify, mock_get_history, mock_spy):
    mock_get_history.side_effect = Exception("Alpaca unreachable")
    from app.pnl import send_monthly_report
    await send_monthly_report()
    msg = mock_notify.call_args[0][0]
    assert "⚠️" in msg
    assert "Monthly" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_yearly_report_success(mock_notify, mock_get_history, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 12000.0])
    from app.pnl import send_yearly_report
    await send_yearly_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Yearly P&L" in msg
    assert "$12,000.00" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_ytd_report_success(mock_notify, mock_get_history, mock_spy):
    mock_get_history.return_value = FakeHistory(equity=[10000.0, 10800.0])
    from app.pnl import send_ytd_report
    await send_ytd_report()
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "YTD" in msg
    assert "$10,800.00" in msg


@pytest.mark.asyncio
@patch("app.pnl.compute_spy_pct", return_value=None)
@patch("app.pnl.get_portfolio_history")
@patch("app.pnl.notify", new_callable=AsyncMock)
async def test_send_alltime_report_success(mock_notify, mock_get_history, mock_spy):
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


@pytest.mark.asyncio
@patch("app.pnl.get_next_trading_day")
@patch("app.pnl.send_monthly_report", new_callable=AsyncMock)
@patch("app.pnl.send_yearly_report", new_callable=AsyncMock)
async def test_check_period_reports_fires_monthly_on_last_trading_day_of_month(
    mock_yearly, mock_monthly, mock_next_day
):
    from datetime import date
    # next trading day is in a different month → fire monthly
    mock_next_day.return_value = date(2026, 6, 1)
    with patch("app.pnl.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 5, 30)
        from app.pnl import check_period_reports
        await check_period_reports()
    mock_monthly.assert_called_once()
    mock_yearly.assert_not_called()


@pytest.mark.asyncio
@patch("app.pnl.get_next_trading_day")
@patch("app.pnl.send_monthly_report", new_callable=AsyncMock)
@patch("app.pnl.send_yearly_report", new_callable=AsyncMock)
async def test_check_period_reports_fires_both_on_last_trading_day_of_year(
    mock_yearly, mock_monthly, mock_next_day
):
    from datetime import date
    # next trading day is in a different year → fire both monthly and yearly
    mock_next_day.return_value = date(2027, 1, 2)
    with patch("app.pnl.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 12, 31)
        from app.pnl import check_period_reports
        await check_period_reports()
    mock_monthly.assert_called_once()
    mock_yearly.assert_called_once()


@pytest.mark.asyncio
@patch("app.pnl.get_next_trading_day")
@patch("app.pnl.send_monthly_report", new_callable=AsyncMock)
@patch("app.pnl.send_yearly_report", new_callable=AsyncMock)
async def test_check_period_reports_silent_mid_month(
    mock_yearly, mock_monthly, mock_next_day
):
    from datetime import date
    # next trading day is same month/year → fire nothing
    mock_next_day.return_value = date(2026, 5, 18)
    with patch("app.pnl.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 5, 15)
        from app.pnl import check_period_reports
        await check_period_reports()
    mock_monthly.assert_not_called()
    mock_yearly.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_pnl.py::test_send_monthly_report_success -v
```
Expected: `ImportError` or `AttributeError` — `send_monthly_report` not defined.

- [ ] **Step 3: Add import and 5 new functions to `app/pnl.py`**

Add `get_next_trading_day` to the existing alpaca_client import line:

```python
from app.trading.alpaca_client import get_latest_price, get_portfolio_history, get_next_trading_day
```

Append the following functions at the bottom of `app/pnl.py`:

```python
async def send_monthly_report() -> None:
    """Fetch monthly portfolio history and post P&L to Discord."""
    now = datetime.now(ET)
    date_str = f"Month of {now.strftime('%B %Y')}"
    try:
        history = get_portfolio_history(period="1M", timeframe="1D")
        result = _compute_pnl(history, "monthly")
        spy_pct = compute_spy_pct("1mo")
        msg = _format_message(result, "Monthly P&L", date_str, spy_pct=spy_pct)
        await notify(msg)
        log.info("Monthly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Monthly P&L report failed: %s", exc)
        await notify(f"⚠️ Monthly P&L report failed: {exc}")


async def send_yearly_report() -> None:
    """Fetch trailing 12-month portfolio history and post P&L to Discord."""
    now = datetime.now(ET)
    date_str = f"Year {now.year}"
    try:
        history = get_portfolio_history(period="1A", timeframe="1D")
        result = _compute_pnl(history, "yearly")
        spy_pct = compute_spy_pct("1y")
        msg = _format_message(result, "Yearly P&L", date_str, spy_pct=spy_pct)
        await notify(msg)
        log.info("Yearly P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("Yearly P&L report failed: %s", exc)
        await notify(f"⚠️ Yearly P&L report failed: {exc}")


async def send_ytd_report() -> None:
    """Fetch year-to-date portfolio history (Jan 1 to today) and post P&L to Discord."""
    now = datetime.now(ET)
    today = now.date()
    jan1 = today.replace(month=1, day=1)
    days = max((today - jan1).days, 1)
    date_str = f"YTD Jan 1–{now.strftime('%b')} {now.day}, {now.year}"
    try:
        history = get_portfolio_history(period=f"{days}D", timeframe="1D")
        result = _compute_pnl(history, "ytd")
        spy_pct = compute_spy_pct("ytd")
        msg = _format_message(result, "YTD P&L", date_str, spy_pct=spy_pct)
        await notify(msg)
        log.info("YTD P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("YTD P&L report failed: %s", exc)
        await notify(f"⚠️ YTD P&L report failed: {exc}")


async def send_alltime_report() -> None:
    """Fetch all-time portfolio history and post P&L to Discord."""
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

        start_str = "inception"
        if start_idx < len(history.timestamp):
            start_dt = datetime.fromtimestamp(history.timestamp[start_idx], tz=ET)
            start_str = start_dt.strftime(f"%b {start_dt.day}, %Y")
        date_str = f"All Time since {start_str}"

        spy_pct = compute_spy_pct("max")
        msg = _format_message(result, "All-Time P&L", date_str, spy_pct=spy_pct)
        await notify(msg)
        log.info("All-time P&L report sent: dollar=%.2f pct=%.2f", result.dollar_pnl, result.pct_pnl)
    except Exception as exc:
        log.error("All-time P&L report failed: %s", exc)
        await notify(f"⚠️ All-time P&L report failed: {exc}")


async def check_period_reports() -> None:
    """Fire monthly/yearly reports when today is the last trading day of the period.

    Called Mon-Fri at 4:05 PM ET by APScheduler. Uses get_next_trading_day()
    to detect month/year boundaries correctly, including market holidays.
    """
    today = datetime.now(ET).date()
    try:
        next_trading = get_next_trading_day()
    except Exception as exc:
        log.warning("Could not fetch next trading day for period check: %s", exc)
        return
    if next_trading.month != today.month:
        await send_monthly_report()
    if next_trading.year != today.year:
        await send_yearly_report()
```

- [ ] **Step 4: Run all new tests to verify they pass**

```
py -m pytest tests/test_pnl.py -v -k "monthly or yearly or ytd or alltime or period"
```
Expected: 8 new tests pass.

- [ ] **Step 5: Run full pnl test suite to check for regressions**

```
py -m pytest tests/test_pnl.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/pnl.py tests/test_pnl.py
git commit -m "feat(pnl): add monthly, yearly, YTD, all-time report functions"
```

---

## Task 2: Register `period_pnl_check` job in `app/scheduler.py`

**Files:**
- Modify: `app/scheduler.py`
- Modify: `tests/test_pnl.py`

**Context:** `app/scheduler.py` currently registers 4 jobs: `daily_pnl`, `weekly_pnl`, `investor_breakdown_daily`, `investor_breakdown_weekly`. The test `test_scheduler_jobs_registered` asserts `len(job_ids) == 4` — this must be updated to 5.

`check_period_reports` is in `app/pnl.py`. Import it alongside the other pnl functions.

- [ ] **Step 1: Update the scheduler job count test**

In `tests/test_pnl.py`, find `test_scheduler_jobs_registered` and update:

```python
def test_scheduler_jobs_registered():
    """setup_jobs() must register the P&L and investor breakdown cron jobs."""
    from app.scheduler import scheduler, setup_jobs
    scheduler.remove_all_jobs()
    setup_jobs()
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert "daily_pnl" in job_ids
    assert "weekly_pnl" in job_ids
    assert "investor_breakdown_daily" in job_ids
    assert "investor_breakdown_weekly" in job_ids
    assert "period_pnl_check" in job_ids
    assert len(job_ids) == 5
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_pnl.py::test_scheduler_jobs_registered -v
```
Expected: FAIL — `period_pnl_check` not in job_ids and len is 4.

- [ ] **Step 3: Update `app/scheduler.py`**

Add `check_period_reports` to the import:

```python
from app.pnl import send_daily_report, send_investor_report, send_weekly_report, check_period_reports
```

Add the new job inside `setup_jobs()` after the existing 4 jobs:

```python
    scheduler.add_job(
        check_period_reports,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=5, timezone=ET),
        id="period_pnl_check",
        replace_existing=True,
    )
```

Also update the log message at the end of `setup_jobs()`:

```python
    log.info("P&L scheduler jobs registered: daily_pnl, weekly_pnl, period_pnl_check (Mon-Fri 16:05 ET)")
```

- [ ] **Step 4: Run test to verify it passes**

```
py -m pytest tests/test_pnl.py::test_scheduler_jobs_registered -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_pnl.py
git commit -m "feat(scheduler): add period_pnl_check job for monthly/yearly auto-reports"
```

---

## Task 3: Update `app/discord_commands.py` handle_report routing

**Files:**
- Modify: `app/discord_commands.py`
- Modify: `tests/test_discord_commands.py`

**Context:** `handle_report` in `app/discord_commands.py` currently routes `"daily"`, `"weekly"`, `"both"`. It imports `send_daily_report` and `send_weekly_report` from `app.pnl`. Add the 4 new functions to the import and the routing.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_discord_commands.py`:

```python
@pytest.mark.asyncio
async def test_handle_report_monthly():
    with patch("app.discord_commands.send_monthly_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("monthly", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "monthly" in msg.lower()


@pytest.mark.asyncio
async def test_handle_report_ytd():
    with patch("app.discord_commands.send_ytd_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("ytd", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_report_1year():
    with patch("app.discord_commands.send_yearly_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("1year", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_handle_report_alltime():
    with patch("app.discord_commands.send_alltime_report", new_callable=AsyncMock), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_report
        await handle_report("alltime", "test-token")
    msg = mock_edit.call_args[0][1]
    assert "✅" in msg
```

- [ ] **Step 2: Run to verify failure**

```
py -m pytest tests/test_discord_commands.py::test_handle_report_monthly -v
```
Expected: FAIL — `send_monthly_report` not importable from `app.discord_commands`.

- [ ] **Step 3: Update `app/discord_commands.py`**

Update the pnl import line:

```python
from app.pnl import (
    send_daily_report,
    send_weekly_report,
    send_monthly_report,
    send_yearly_report,
    send_ytd_report,
    send_alltime_report,
)
```

Update `handle_report`:

```python
async def handle_report(report_type: str, token: str) -> None:
    if report_type in ("daily", "both"):
        await send_daily_report()
    if report_type in ("weekly", "both"):
        await send_weekly_report()
    if report_type == "monthly":
        await send_monthly_report()
    if report_type == "ytd":
        await send_ytd_report()
    if report_type == "1year":
        await send_yearly_report()
    if report_type == "alltime":
        await send_alltime_report()
    await _edit_original(token, f"✅ {report_type.capitalize()} report sent")
```

- [ ] **Step 4: Run tests to verify they pass**

```
py -m pytest tests/test_discord_commands.py -v
```
Expected: all 12 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/discord_commands.py tests/test_discord_commands.py
git commit -m "feat(discord): route monthly, ytd, 1year, alltime report commands"
```

---

## Task 4: Update `app/main.py` `/run-report` endpoint

**Files:**
- Modify: `app/main.py`

**Context:** The `/run-report` endpoint currently only accepts `"daily"`, `"weekly"`, `"both"`. Update the import and routing to match the new Discord command types.

- [ ] **Step 1: Update the pnl import in `app/main.py`**

Find the existing line:
```python
from app.pnl import send_daily_report, send_weekly_report
```

Replace with:
```python
from app.pnl import (
    send_daily_report,
    send_weekly_report,
    send_monthly_report,
    send_yearly_report,
    send_ytd_report,
    send_alltime_report,
)
```

- [ ] **Step 2: Update the `/run-report` route body**

Find the existing route body:
```python
    report = body.get("report", "daily")
    if report not in ("daily", "weekly", "both"):
        return JSONResponse(status_code=422, content={"error": "report must be 'daily', 'weekly', or 'both'"})

    if report in ("daily", "both"):
        await send_daily_report()
    if report in ("weekly", "both"):
        await send_weekly_report()

    return {"status": "ok", "report": report}
```

Replace with:
```python
    _VALID = {"daily", "weekly", "monthly", "ytd", "1year", "alltime", "both"}
    report = body.get("report", "daily")
    if report not in _VALID:
        return JSONResponse(
            status_code=422,
            content={"error": f"report must be one of: {', '.join(sorted(_VALID))}"},
        )

    if report in ("daily", "both"):
        await send_daily_report()
    if report in ("weekly", "both"):
        await send_weekly_report()
    if report == "monthly":
        await send_monthly_report()
    if report == "ytd":
        await send_ytd_report()
    if report == "1year":
        await send_yearly_report()
    if report == "alltime":
        await send_alltime_report()

    return {"status": "ok", "report": report}
```

- [ ] **Step 3: Run full test suite to verify no regressions**

```
py -m pytest tests/ -v
```
Expected: all pass (1 pre-existing failure in `test_github_commit.py::test_commit_raises_on_get_failure` is unrelated — ignore it).

- [ ] **Step 4: Commit**

```bash
git add app/main.py
git commit -m "feat(api): expand /run-report to support monthly, ytd, 1year, alltime"
```

---

## Task 5: Update `scripts/register_commands.py` and push

**Files:**
- Modify: `scripts/register_commands.py`

**Context:** The `/report` command currently has 3 choices: `daily`, `weekly`, `both`. Add 4 new choices. After pushing, re-run the script to update Discord.

- [ ] **Step 1: Update choices in `scripts/register_commands.py`**

Find the report command's options list and replace the `choices` array:

```python
                "choices": [
                    {"name": "Daily", "value": "daily"},
                    {"name": "Weekly", "value": "weekly"},
                    {"name": "Monthly", "value": "monthly"},
                    {"name": "Year to Date", "value": "ytd"},
                    {"name": "1 Year", "value": "1year"},
                    {"name": "All Time", "value": "alltime"},
                    {"name": "Daily & Weekly", "value": "both"},
                ],
```

- [ ] **Step 2: Commit and push**

```bash
git add scripts/register_commands.py
git commit -m "chore: add monthly, ytd, 1year, alltime choices to /report slash command"
git push
```

- [ ] **Step 3: Re-register Discord commands**

After Render deploys (wait for green health check), run:

```powershell
$env:DISCORD_APP_ID="your_app_id"; $env:DISCORD_BOT_TOKEN="your_bot_token"; py scripts/register_commands.py
```

Expected:
```
✅ Registered 3 commands successfully
  /deposit
  /withdraw
  /report
```

The `/report` command will now show 7 choices in Discord autocomplete.

---

## Self-Review

**Spec coverage:**
- ✅ `send_monthly_report` — Task 1
- ✅ `send_yearly_report` — Task 1
- ✅ `send_ytd_report` — Task 1
- ✅ `send_alltime_report` — Task 1
- ✅ `check_period_reports` — Task 1
- ✅ `period_pnl_check` scheduled job Mon–Fri 4:05 PM ET — Task 2
- ✅ Monthly fires on last trading day of month — Task 1 (`check_period_reports`)
- ✅ Yearly fires on last trading day of Dec — Task 1 (`check_period_reports`)
- ✅ YTD and all-time are on-demand only — Task 3 and 4 (no scheduler entry)
- ✅ Discord `/report` routing — Task 3
- ✅ `/run-report` HTTP endpoint — Task 4
- ✅ Discord slash command choices updated — Task 5
- ✅ All-time skips leading zero equity — Task 1 (`send_alltime_report` uses `start_idx`)

**Type consistency:**
- `send_monthly_report()`, `send_yearly_report()`, `send_ytd_report()`, `send_alltime_report()`, `check_period_reports()` — all `async def ... -> None`, consistent Tasks 1–3
- `handle_report` routes `"monthly"` → `send_monthly_report()`, `"ytd"` → `send_ytd_report()`, `"1year"` → `send_yearly_report()`, `"alltime"` → `send_alltime_report()` — consistent Tasks 3–4
- `/run-report` uses same string values as Discord: `"monthly"`, `"ytd"`, `"1year"`, `"alltime"` — consistent Task 4
