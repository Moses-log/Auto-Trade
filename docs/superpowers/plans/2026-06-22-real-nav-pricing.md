# Real NAV-Per-Unit Pricing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the raw-SPY-price benchmark used to value investor equity and
cap withdrawals with a real NAV-per-unit derived from the fund's actual Alpaca
account equity, so the Discord investor breakdown matches what's really in the
account and nobody can withdraw more than their true proportional share.

**Architecture:** A single new pure function, `compute_nav_per_unit()`, computes
`real_total_equity / total_units_outstanding`. Every place that currently values
SPY-units at raw SPY price (`compute_breakdown`, `compute_withdrawal_lots`,
`format_withdrawal_message`, and new-deposit pricing in `handle_deposit`/`POST
/deposit`) switches to valuing them at this NAV instead. `entry_spy` (original
cost basis per deposit) and all FIFO tax-lot/holding-period logic are untouched.

**Tech Stack:** Python 3.9, FastAPI, pytest + pytest-asyncio, alpaca-py SDK.

## Global Constraints

- `entry_spy` on `Deposit` records is never redefined, migrated, or rewritten
  for existing data — cost-basis math (`consume_units * entry_spy`) stays
  self-consistent regardless of what price source funded that field.
- `compute_time_weighted_capital()` and `app/tax.py` are untouched — they
  already operate purely on deposit/withdrawal dollar amounts.
- `get_account()` (in `app/trading/alpaca_client.py`) is never modified — it
  raises on failure (after its existing `@_retry` retries) rather than
  returning `None`, unlike `get_latest_price()`. Every new call site must
  handle that with its own `try/except`.
- Manual `spy_price` overrides on deposits (existing behavior) are preserved
  exactly — NAV auto-pricing only applies to the live/automatic deposit path.
- No schema change to `investors.json` — `Deposit`/`Withdrawal` dataclass
  fields are unchanged.

---

### Task 1: `compute_nav_per_unit()` helper

**Files:**
- Modify: `app/investors.py` (insert after `_net_units`, currently lines 118-122)
- Test: `tests/test_investors.py`

**Interfaces:**
- Produces: `compute_nav_per_unit(investors: list[Investor], real_total_equity: float) -> float` — used by Tasks 2, 3, and 4.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_investors.py` (anywhere after the imports; suggested
location is right before `test_compute_breakdown_single_deposit`):

```python
def test_compute_nav_per_unit_single_investor():
    from app.investors import Deposit, Investor, compute_nav_per_unit
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=600.0, date="2026-01-01")])
    ]
    # net_units = 300/600 = 0.5; real equity = 350 -> nav_per_unit = 700
    assert compute_nav_per_unit(investors, real_total_equity=350.0) == pytest.approx(700.0)


def test_compute_nav_per_unit_sums_units_across_investors():
    from app.investors import Deposit, Investor, compute_nav_per_unit
    investors = [
        Investor(name="A", deposits=[Deposit(amount=300.0, entry_spy=600.0, date="2026-01-01")]),  # 0.5 units
        Investor(name="B", deposits=[Deposit(amount=600.0, entry_spy=600.0, date="2026-01-01")]),  # 1.0 units
    ]
    # total units = 1.5; real equity = 1500 -> nav_per_unit = 1000
    assert compute_nav_per_unit(investors, real_total_equity=1500.0) == pytest.approx(1000.0)


def test_compute_nav_per_unit_returns_zero_when_no_units_outstanding():
    from app.investors import compute_nav_per_unit
    assert compute_nav_per_unit([], real_total_equity=5000.0) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_investors.py -v -k compute_nav_per_unit`
Expected: `ImportError: cannot import name 'compute_nav_per_unit'` (3 failed)

- [ ] **Step 3: Implement `compute_nav_per_unit()`**

In `app/investors.py`, insert immediately after the `_net_units` function
(currently ends at line 122, right before the blank lines preceding
`compute_time_weighted_capital`):

```python
def compute_nav_per_unit(investors: list[Investor], real_total_equity: float) -> float:
    """Real dollar value of one SPY-unit, grounded in actual Alpaca equity
    rather than raw SPY market price.

    Returns 0.0 if no units are outstanding (NAV is undefined with zero units;
    callers must handle this as a bootstrap case, not divide-by-zero).
    """
    total_units = sum(_net_units(inv) for inv in investors)
    if total_units <= 0:
        return 0.0
    return real_total_equity / total_units
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_investors.py -v -k compute_nav_per_unit`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add app/investors.py tests/test_investors.py
git commit -m "feat: add compute_nav_per_unit, real-equity pricing helper"
```

---

### Task 2: Real NAV for the investor breakdown report

**Files:**
- Modify: `app/investors.py:190-229` (`compute_breakdown`)
- Modify: `app/pnl.py:722-751` (`send_investor_report`)
- Test: `tests/test_investors.py`

**Interfaces:**
- Consumes: `compute_nav_per_unit(investors, real_total_equity) -> float` (Task 1)
- Produces: `compute_breakdown(investors, spy_price, real_total_equity) -> InvestorBreakdown` — new required third parameter. `format_discord_message` is unchanged (still reads `breakdown.spy_price` for the header line and `breakdown.total_portfolio` etc. for the rest — no signature change needed since the breakdown object already carries everything).

- [ ] **Step 1: Update existing `compute_breakdown` tests for the new signature**

In `tests/test_investors.py`, replace these five test bodies. Each adds a
`real_total_equity` argument set to match what the OLD raw-SPY-price model
would have produced, so every existing dollar assertion stays valid — this
proves the refactor is behavior-preserving when real equity happens to equal
the old synthetic total, before the next step adds tests that prove it also
works correctly when they *differ* (the actual point of this task).

```python
def test_compute_breakdown_single_deposit():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    # net_units = 0.6; real_total_equity=360.0 matches the old spy_price=600 synthetic total exactly
    result = compute_breakdown(investors, spy_price=600.0, real_total_equity=360.0)
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
    result = compute_breakdown(investors, spy_price=110.0, real_total_equity=2200.0)
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
    # Old synthetic total at spy_price=600: 300*600/500 + 500*600/600 = 860.0
    result = compute_breakdown(investors, spy_price=600.0, real_total_equity=860.0)
    assert result.investors[0].current_equity == pytest.approx(860.0)
    assert result.investors[0].total_deposited == pytest.approx(800.0)
    assert result.investors[0].dollar_pnl == pytest.approx(60.0)


def test_compute_breakdown_totals():
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
        Investor(name="B", deposits=[Deposit(amount=2000.0, entry_spy=100.0, date="2026-01-01")]),
    ]
    result = compute_breakdown(investors, spy_price=110.0, real_total_equity=3300.0)
    assert result.total_deposited == pytest.approx(3000.0)
    assert result.total_portfolio == pytest.approx(3300.0)
    assert result.overall_dollar_pnl == pytest.approx(300.0)
    assert result.overall_pct_pnl == pytest.approx(10.0)
    assert result.spy_price == 110.0
```

And update the four `format_discord_message` tests' `compute_breakdown` calls
(only the call line changes in each):

```python
def test_format_discord_message_contains_investor_name_and_date():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0, real_total_equity=360.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "Moses" in msg
    assert "May 9, 2026" in msg


def test_format_discord_message_shows_current_equity():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0, real_total_equity=360.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "360.00" in msg  # current_equity = real_total_equity for this single investor


def test_format_discord_message_prefixes_positive_pnl_with_plus():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    breakdown = compute_breakdown(investors, spy_price=600.0, real_total_equity=360.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "+$60.00" in msg


def test_format_discord_message_shows_totals():
    from app.investors import Deposit, Investor, compute_breakdown, format_discord_message
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),
        Investor(name="B", deposits=[Deposit(amount=2000.0, entry_spy=100.0, date="2026-01-01")]),
    ]
    breakdown = compute_breakdown(investors, spy_price=110.0, real_total_equity=3300.0)
    msg = format_discord_message(breakdown, "May 9, 2026")
    assert "3,300.00" in msg  # total portfolio
    assert "3,000.00" in msg  # total deposited
```

- [ ] **Step 2: Add new tests proving the actual fix**

Add to `tests/test_investors.py`, right after the updated `test_compute_breakdown_totals`:

```python
def test_compute_breakdown_total_portfolio_matches_real_equity_not_synthetic_spy_total():
    """The point of the fix: Total Portfolio must equal the real Alpaca account
    equity, even when that differs from what raw-SPY-price valuation of the
    same units would imply."""
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    # Old synthetic model would value this at (300/500)*600 = 360.0.
    # The fund's real equity is actually 420.0 -- it outperformed raw SPY.
    result = compute_breakdown(investors, spy_price=600.0, real_total_equity=420.0)
    assert result.total_portfolio == pytest.approx(420.0)
    assert result.investors[0].current_equity == pytest.approx(420.0)
    assert result.investors[0].dollar_pnl == pytest.approx(120.0)  # 420 - 300 cost basis


def test_compute_breakdown_portfolio_share_independent_of_nav_value():
    """Ownership proportions are driven by unit counts, not by what
    real_total_equity happens to be -- changing it must not change the split."""
    from app.investors import Deposit, Investor, compute_breakdown
    investors = [
        Investor(name="A", deposits=[Deposit(amount=1000.0, entry_spy=100.0, date="2026-01-01")]),  # 10 units
        Investor(name="B", deposits=[Deposit(amount=3000.0, entry_spy=100.0, date="2026-01-01")]),  # 30 units
    ]
    result = compute_breakdown(investors, spy_price=110.0, real_total_equity=8000.0)
    assert result.investors[0].portfolio_share == pytest.approx(25.0)
    assert result.investors[1].portfolio_share == pytest.approx(75.0)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `py -m pytest tests/test_investors.py -v -k "compute_breakdown or format_discord_message"`
Expected: `TypeError: compute_breakdown() missing 1 required positional argument: 'real_total_equity'` across all of them.

- [ ] **Step 4: Update `compute_breakdown` in `app/investors.py`**

Replace the existing function (lines 190-229):

```python
def compute_breakdown(
    investors: list[Investor],
    spy_price: float,
    real_total_equity: float,
) -> InvestorBreakdown:
    nav_per_unit = compute_nav_per_unit(investors, real_total_equity)
    results: list[InvestorResult] = []
    for inv in investors:
        current_equity = _net_units(inv) * nav_per_unit

        gross_deposited = sum(d.amount for d in inv.deposits)
        withdrawn_basis = sum(w.cost_basis for w in inv.withdrawals)
        withdrawn_proceeds = sum(w.proceeds for w in inv.withdrawals)
        net_cost_basis = gross_deposited - withdrawn_basis

        dollar_pnl = current_equity - net_cost_basis
        pct_pnl = (dollar_pnl / net_cost_basis * 100) if net_cost_basis else 0.0
        results.append(
            InvestorResult(
                name=inv.name,
                total_deposited=net_cost_basis,
                current_equity=current_equity,
                dollar_pnl=dollar_pnl,
                pct_pnl=pct_pnl,
                portfolio_share=0.0,
                total_withdrawn=withdrawn_proceeds,
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

(Only the signature and the first line of the loop body changed —
`_net_units(inv) * spy_price` became `_net_units(inv) * nav_per_unit`, with
`nav_per_unit` computed once up front. `spy_price` is kept on the dataclass
purely so `format_discord_message` can still show the `SPY: $X` header line.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `py -m pytest tests/test_investors.py -v -k "compute_breakdown or format_discord_message"`
Expected: all pass (11 tests: 5 updated `compute_breakdown` + 2 new + 4 `format_discord_message`)

- [ ] **Step 6: Update `send_investor_report` in `app/pnl.py`**

Replace lines 722-751:

```python
async def send_investor_report() -> None:
    investors = load_investors()
    if not investors:
        log.warning("No investors found; skipping investor report")
        return

    spy_price = get_latest_price("SPY")
    if spy_price is None:
        log.warning("Could not fetch SPY price; skipping investor report")
        return

    try:
        account = get_account()
        real_total_equity = float(account.equity)
    except Exception as exc:
        log.warning("Could not fetch account equity; skipping investor report: %s", exc)
        return

    now = datetime.now(ET)
    date_str = now.strftime(f"%B {now.day}, %Y")
    try:
        breakdown = compute_breakdown(investors, spy_price, real_total_equity)
        message = format_discord_message(breakdown, date_str)
        chart_bytes = None
        try:
            loop = asyncio.get_running_loop()
            chart_bytes = await loop.run_in_executor(None, generate_investor_pie_chart, breakdown, date_str)
        except Exception as exc:
            log.warning("Investor pie chart generation failed: %s", exc)
        if chart_bytes:
            await notify_investors_with_chart(message, chart_bytes)
        else:
            await notify_investors(message)
        log.info("Investor report sent for %s", date_str)
    except Exception as exc:
        log.error("Investor report failed: %s", exc)
        await notify_investors(f"⚠️ Investor report failed: {exc}")
```

`get_account` is already imported at the top of `app/pnl.py` (line 25) — no
import changes needed in this file.

- [ ] **Step 7: Update existing `send_investor_report` tests**

In `tests/test_investors.py`, both `test_send_investor_report_sends_chart_with_investor_name`
and `test_send_investor_report_falls_back_to_text_when_no_chart` need an added
`get_account` patch. Replace both test bodies:

```python
@pytest.mark.asyncio
async def test_send_investor_report_sends_chart_with_investor_name():
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.investors import Deposit, Investor
    mock_investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    mock_account = MagicMock()
    mock_account.equity = "360.00"  # matches old spy_price=600 synthetic total (300/500*600)
    with patch("app.pnl.load_investors", return_value=mock_investors):
        with patch("app.pnl.get_latest_price", return_value=600.0):
            with patch("app.pnl.get_account", return_value=mock_account):
                with patch("app.pnl.generate_investor_pie_chart", return_value=b"\x89PNG_fake"):
                    with patch("app.pnl.notify_investors_with_chart", new_callable=AsyncMock) as mock_notify_chart:
                        with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
                            from app.pnl import send_investor_report
                            await send_investor_report()
    mock_notify_chart.assert_called_once()
    mock_notify.assert_not_called()
    message, chart_bytes = mock_notify_chart.call_args[0]
    assert "Moses" in message
    assert "360.00" in message
    assert chart_bytes == b"\x89PNG_fake"


@pytest.mark.asyncio
async def test_send_investor_report_falls_back_to_text_when_no_chart():
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.investors import Deposit, Investor
    mock_investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    mock_account = MagicMock()
    mock_account.equity = "360.00"
    with patch("app.pnl.load_investors", return_value=mock_investors):
        with patch("app.pnl.get_latest_price", return_value=600.0):
            with patch("app.pnl.get_account", return_value=mock_account):
                with patch("app.pnl.generate_investor_pie_chart", return_value=b""):
                    with patch("app.pnl.notify_investors_with_chart", new_callable=AsyncMock) as mock_notify_chart:
                        with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
                            from app.pnl import send_investor_report
                            await send_investor_report()
    mock_notify_chart.assert_not_called()
    mock_notify.assert_called_once()
    message = mock_notify.call_args[0][0]
    assert "Moses" in message
    assert "360.00" in message
```

(`test_send_investor_report_skips_when_no_investors` and
`test_send_investor_report_skips_when_spy_price_unavailable` are unaffected —
both return before `get_account` would ever be called.)

- [ ] **Step 8: Add a new test for the account-equity-unavailable case**

Add to `tests/test_investors.py`, right after `test_send_investor_report_skips_when_spy_price_unavailable`:

```python
@pytest.mark.asyncio
async def test_send_investor_report_skips_when_account_equity_unavailable():
    from unittest.mock import AsyncMock, patch
    from app.investors import Deposit, Investor
    mock_investors = [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")])
    ]
    with patch("app.pnl.load_investors", return_value=mock_investors):
        with patch("app.pnl.get_latest_price", return_value=600.0):
            with patch("app.pnl.get_account", side_effect=RuntimeError("API down")):
                with patch("app.pnl.notify_investors", new_callable=AsyncMock) as mock_notify:
                    from app.pnl import send_investor_report
                    await send_investor_report()
    mock_notify.assert_not_called()
```

- [ ] **Step 9: Run the full investors/pnl test slice**

Run: `py -m pytest tests/test_investors.py -v -k "compute_breakdown or format_discord_message or send_investor_report"`
Expected: all pass (14 tests)

- [ ] **Step 10: Commit**

```bash
git add app/investors.py app/pnl.py tests/test_investors.py
git commit -m "feat: price investor breakdown report on real account equity"
```

---

### Task 3: Real NAV for withdrawal caps and proceeds

**Files:**
- Modify: `app/investors.py:232-297` (`compute_withdrawal_lots`)
- Modify: `app/investors.py:300-395` (`format_withdrawal_message`)
- Modify: `app/withdrawal_execution.py` (`schedule_withdrawal`, `execute_pending_withdrawal`, imports)
- Test: `tests/test_investors.py`, `tests/test_withdraw.py`, `tests/test_withdrawal_execution.py`, `tests/test_discord_commands.py`

**Interfaces:**
- Consumes: `compute_nav_per_unit(investors, real_total_equity) -> float` (Task 1)
- Produces: `compute_withdrawal_lots(investor, withdraw_amount, nav_per_unit) -> tuple[list[dict], float]` — `current_spy` param renamed/repurposed to `nav_per_unit`. `format_withdrawal_message(investor, lots, units_redeemed, current_spy, nav_per_unit, withdraw_amount) -> str` — new `nav_per_unit` parameter inserted before `withdraw_amount`.

This task changes `compute_withdrawal_lots`'s signature and every one of its
callers in the same task, since splitting them would leave the test suite
broken between sub-steps.

- [ ] **Step 1: Write new direct unit tests for `compute_withdrawal_lots`**

There are currently no direct unit tests for this function (only indirect
coverage via `withdrawal_execution.py`'s tests) — add them now since we're
changing its math. Add to `tests/test_investors.py`, right after the
`test_compute_breakdown_portfolio_share_independent_of_nav_value` test added
in Task 2:

```python
def test_compute_withdrawal_lots_single_lot_full_math():
    from app.investors import Deposit, Investor, compute_withdrawal_lots
    investor = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")
    ])
    # net_units = 0.6; nav_per_unit = 700 -> available_equity = 420.0
    lots, units_redeemed = compute_withdrawal_lots(investor, 210.0, nav_per_unit=700.0)
    assert units_redeemed == pytest.approx(0.3)  # 210 / 700
    assert len(lots) == 1
    lot = lots[0]
    assert lot["units"] == pytest.approx(0.3)
    assert lot["cost"] == pytest.approx(150.0)      # 0.3 * 500 entry_spy (unchanged cost basis)
    assert lot["proceeds"] == pytest.approx(210.0)  # 0.3 * 700 nav_per_unit (real proceeds)
    assert lot["gain"] == pytest.approx(60.0)
    assert lot["entry_spy"] == 500.0


def test_compute_withdrawal_lots_raises_when_amount_exceeds_real_equity():
    from app.investors import Deposit, Investor, compute_withdrawal_lots
    investor = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01")
    ])
    with pytest.raises(ValueError):
        compute_withdrawal_lots(investor, 500.0, nav_per_unit=700.0)  # available = 420


def test_compute_withdrawal_lots_fifo_across_multiple_deposits():
    from app.investors import Deposit, Investor, compute_withdrawal_lots
    investor = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=500.0, date="2026-01-01"),  # 0.6 units
        Deposit(amount=400.0, entry_spy=800.0, date="2026-02-01"),  # 0.5 units
    ])
    # total units = 1.1; nav_per_unit = 1000 -> available_equity = 1100.0
    # withdraw 1000 -> units_to_redeem = 1.0 -> consumes all of lot 1 (0.6) + 0.4 of lot 2
    lots, units_redeemed = compute_withdrawal_lots(investor, 1000.0, nav_per_unit=1000.0)
    assert units_redeemed == pytest.approx(1.0)
    assert len(lots) == 2
    assert lots[0]["units"] == pytest.approx(0.6)
    assert lots[0]["cost"] == pytest.approx(300.0)
    assert lots[1]["units"] == pytest.approx(0.4)
    assert lots[1]["cost"] == pytest.approx(320.0)  # 0.4 * 800


def test_compute_withdrawal_lots_rejects_amount_old_model_would_have_allowed():
    """The actual financial-safety fix: under the old raw-SPY-price model, a
    $2,200 withdrawal here would have been allowed (synthetic equity at
    SPY=$800 is $2,285.71), but the fund's real equity is only $1,500 -- the
    new nav_per_unit-based cap correctly rejects it."""
    from app.investors import Deposit, Investor, compute_withdrawal_lots
    investor = Investor(name="Moses", deposits=[
        Deposit(amount=2000.0, entry_spy=700.0, date="2026-01-01")
    ])
    nav_per_unit = 525.0  # real equity $1,500 / 2.857142857 units
    with pytest.raises(ValueError) as exc_info:
        compute_withdrawal_lots(investor, 2200.0, nav_per_unit)
    assert "exceeds available equity $1,500.00" in str(exc_info.value)


def test_format_withdrawal_message_shows_real_remaining_equity():
    from app.investors import Deposit, Investor, compute_withdrawal_lots, format_withdrawal_message
    investor = Investor(name="Moses", deposits=[
        Deposit(amount=1000.0, entry_spy=500.0, date="2026-01-01")  # 2.0 units
    ])
    # nav_per_unit = 600 -> available_equity = 1200; withdraw 600 -> units_redeemed=1.0
    lots, units_redeemed = compute_withdrawal_lots(investor, 600.0, nav_per_unit=600.0)
    msg = format_withdrawal_message(
        investor, lots, units_redeemed,
        current_spy=550.0, nav_per_unit=600.0, withdraw_amount=600.0,
    )
    assert "Moses" in msg
    # remaining_units = 2.0 - 1.0 = 1.0; remaining_equity must use nav_per_unit (600),
    # not current_spy (550) -- proves the math uses real NAV, not raw SPY price.
    assert "600.00" in msg
    assert "550.00" in msg  # SPY header still shown for market context
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_investors.py -v -k "compute_withdrawal_lots or format_withdrawal_message"`
Expected: `TypeError` on the keyword argument (`nav_per_unit` not recognized — function still takes `current_spy`).

- [ ] **Step 3: Update `compute_withdrawal_lots` in `app/investors.py`**

Replace the existing function (lines 232-297):

```python
def compute_withdrawal_lots(
    investor: Investor,
    withdraw_amount: float,
    nav_per_unit: float,
) -> tuple[list[dict], float]:
    """FIFO-match a dollar withdrawal against the investor's deposit lots.

    Returns (lots, units_redeemed).
    Each lot: cost, proceeds, gain, units, short_term, entry_date, entry_spy, holding_days.

    Prior withdrawals are respected — their consumed units are skipped FIFO-style
    so each lot is only redeemed once across the investor's full withdrawal history.

    `nav_per_unit` is the fund's real NAV per unit (see compute_nav_per_unit),
    not the raw SPY market price — this determines both the withdrawal cap and
    the real dollar proceeds per lot. `entry_spy` (cost basis) is unaffected.
    """
    today = datetime.now(_ET).date()
    available_equity = _net_units(investor) * nav_per_unit

    if withdraw_amount > available_equity + 0.005:
        raise ValueError(
            f"Withdrawal ${withdraw_amount:,.2f} exceeds available equity "
            f"${available_equity:,.2f}"
        )

    units_to_redeem = withdraw_amount / nav_per_unit
    lots: list[dict] = []
    remaining = units_to_redeem

    # Units already consumed by prior withdrawals, applied FIFO across deposits
    prior_consumed = sum(w.units for w in investor.withdrawals)

    for d in investor.deposits:
        if remaining <= 1e-9:
            break
        if not d.entry_spy:
            continue

        deposit_total_units = d.amount / d.entry_spy
        already_consumed = min(prior_consumed, deposit_total_units)
        prior_consumed = max(0.0, prior_consumed - already_consumed)
        available_from_lot = deposit_total_units - already_consumed

        if available_from_lot <= 1e-9:
            continue

        consume = min(remaining, available_from_lot)
        lot_cost = consume * d.entry_spy
        lot_proceeds = consume * nav_per_unit

        try:
            entry_date = _date.fromisoformat(d.date)
        except (ValueError, TypeError):
            entry_date = today
        holding_days = (today - entry_date).days

        lots.append({
            "cost":         lot_cost,
            "proceeds":     lot_proceeds,
            "gain":         lot_proceeds - lot_cost,
            "units":        consume,
            "short_term":   holding_days < 365,
            "entry_date":   d.date,
            "entry_spy":    d.entry_spy,
            "holding_days": holding_days,
        })
        remaining -= consume

    return lots, units_to_redeem
```

- [ ] **Step 4: Update `format_withdrawal_message` in `app/investors.py`**

Replace the signature (lines 300-306) and the remaining-equity line (line 334):

```python
def format_withdrawal_message(
    investor: Investor,
    lots: list[dict],
    units_redeemed: float,
    current_spy: float,
    nav_per_unit: float,
    withdraw_amount: float,
) -> str:
```

And change line 334 from:

```python
    remaining_equity  = remaining_units * current_spy
```

to:

```python
    remaining_equity  = remaining_units * nav_per_unit
```

Every other line in this function (the header's `SPY @ ${current_spy:,.2f}`,
the FIFO lot listing using `lot['entry_spy']`, the tax estimate, etc.) is
unchanged — `current_spy` is still used for display, `nav_per_unit` only for
the remaining-position dollar figure.

- [ ] **Step 5: Run the investors.py test slice to verify it passes**

Run: `py -m pytest tests/test_investors.py -v -k "compute_withdrawal_lots or format_withdrawal_message"`
Expected: `5 passed`

- [ ] **Step 6: Update `app/withdrawal_execution.py` imports**

Change the imports block at the top of the file from:

```python
from app.investors import (
    Withdrawal,
    compute_withdrawal_lots,
    format_withdrawal_message,
    load_investors,
    save_investors,
    investors_lock,
)
```

to:

```python
from app.investors import (
    Withdrawal,
    compute_nav_per_unit,
    compute_withdrawal_lots,
    format_withdrawal_message,
    load_investors,
    save_investors,
    investors_lock,
)
```

And change:

```python
from app.trading.alpaca_client import get_latest_price
```

to:

```python
from app.trading.alpaca_client import get_account, get_latest_price
```

- [ ] **Step 7: Update `schedule_withdrawal` in `app/withdrawal_execution.py`**

Replace the function body between the investor-lookup check and the
`compute_withdrawal_lots` call:

```python
    investors = load_investors()
    inv = next((i for i in investors if i.name.lower() == investor_name.lower()), None)
    if inv is None:
        raise WithdrawalValidationError(f'Investor "{investor_name}" not found — check spelling')

    try:
        account = get_account()
        real_total_equity = float(account.equity)
    except Exception as exc:
        raise WithdrawalValidationError("Could not fetch account equity — try again") from exc
    nav_per_unit = compute_nav_per_unit(investors, real_total_equity)

    try:
        # Validation only — the result is discarded. Execution re-runs this with
        # a live price/equity reading and the investor's state at execution time, not now.
        compute_withdrawal_lots(inv, amount, nav_per_unit)
    except ValueError as exc:
        raise WithdrawalValidationError(str(exc)) from exc
```

(This replaces the old `compute_withdrawal_lots(inv, amount, spy_price)` call.
`get_account()` is fetched only after the investor is confirmed to exist, so
an unknown-investor request never needs account data.)

- [ ] **Step 8: Update `execute_pending_withdrawal` in `app/withdrawal_execution.py`**

Replace the price-fetch-and-retry block (originally lines 107-130) with:

```python
    spy_price = get_latest_price("SPY")
    real_total_equity = None
    if spy_price is not None:
        try:
            account = get_account()
            real_total_equity = float(account.equity)
        except Exception as exc:
            log.warning(
                "execute_pending_withdrawal: could not fetch account equity for %s: %s",
                withdrawal_id, exc,
            )
            real_total_equity = None

    if spy_price is None or real_total_equity is None:
        log.error(
            "execute_pending_withdrawal: market/account data unavailable for %s — retrying in 15 minutes",
            withdrawal_id,
        )
        retry_at = datetime.now(_CT) + timedelta(minutes=15)
        try:
            scheduler.add_job(
                execute_pending_withdrawal,
                "date",
                run_date=retry_at,
                args=[withdrawal_id],
                id=f"withdrawal_{withdrawal_id}",
                replace_existing=True,
            )
        except Exception:
            log.exception("execute_pending_withdrawal: failed to schedule retry for %s", withdrawal_id)
        try:
            await notify_investors(
                f"⚠️ Scheduled withdrawal for {record['investor']} (${record['amount']:,.2f}) "
                f"could not execute — market data unavailable. Retrying at "
                f"{retry_at.strftime('%b %d, %I:%M %p %Z')}."
            )
        except Exception:
            log.exception("execute_pending_withdrawal: failed to send price-unavailable notification for %s", withdrawal_id)
        return
```

Then, inside the `async with investors_lock:` block, replace:

```python
            try:
                lots, units_redeemed = compute_withdrawal_lots(inv, record["amount"], spy_price)
            except ValueError as exc:
                error_reason = str(exc)
            else:
                try:
                    total_cost_basis = sum(lot["cost"] for lot in lots)
                    discord_msg = format_withdrawal_message(inv, lots, units_redeemed, spy_price, record["amount"])
```

with:

```python
            nav_per_unit = compute_nav_per_unit(investors, real_total_equity)
            try:
                lots, units_redeemed = compute_withdrawal_lots(inv, record["amount"], nav_per_unit)
            except ValueError as exc:
                error_reason = str(exc)
            else:
                try:
                    total_cost_basis = sum(lot["cost"] for lot in lots)
                    discord_msg = format_withdrawal_message(
                        inv, lots, units_redeemed, spy_price, nav_per_unit, record["amount"]
                    )
```

Everything else in `execute_pending_withdrawal` (the `Withdrawal(...)` record
construction using `exit_spy=spy_price`, the audit/notify/backup calls) is
unchanged.

- [ ] **Step 9: Update existing `withdrawal_execution.py` tests**

In `tests/test_withdrawal_execution.py`, add a `get_account` patch to every
test that reaches the investor-lookup-succeeds path. The mock equity is set
to equal the investor's total deposited dollars, which makes `nav_per_unit`
come out to exactly the investor's `entry_spy` (since `equity / (amount /
entry_spy) == entry_spy` when `equity == amount`) — this keeps every existing
assertion numerically unchanged while still exercising the new code path.

Add this helper near the top of the file, after `_moses`:

```python
def _mock_account(equity: float):
    from unittest.mock import MagicMock
    account = MagicMock()
    account.equity = str(equity)
    return account
```

Update these five tests by adding a `get_account` patch line to each `with`
block (the assertions in each test are unchanged):

`test_schedule_withdrawal_rejects_amount_exceeding_equity` — investor has
`deposits_amount=300.0`, so add:
```python
         patch("app.withdrawal_execution.get_account", return_value=_mock_account(300.0)),
```

`test_schedule_withdrawal_saves_pending_and_adds_scheduler_job` — investor has
the default `deposits_amount=2000.0`, so add:
```python
         patch("app.withdrawal_execution.get_account", return_value=_mock_account(2000.0)),
```

`test_schedule_withdrawal_run_at_respects_delay_setting` — same default
2000.0 investor, add the same patch as above.

`test_execute_pending_withdrawal_writes_to_investors_and_audits_executed` —
default 2000.0 investor, add the same `_mock_account(2000.0)` patch.

`test_execute_pending_withdrawal_audits_failed_when_equity_insufficient` —
investor has `deposits_amount=100.0`, add:
```python
         patch("app.withdrawal_execution.get_account", return_value=_mock_account(100.0)),
```

`test_execute_pending_withdrawal_audits_failed_when_save_investors_raises` —
default 2000.0 investor, add the same `_mock_account(2000.0)` patch.

(`test_schedule_withdrawal_rejects_non_positive_amount`,
`test_schedule_withdrawal_rejects_unknown_investor`,
`test_execute_pending_withdrawal_returns_silently_when_record_missing`,
`test_execute_pending_withdrawal_retries_and_notifies_when_price_unavailable`,
and all three `cancel_pending_withdrawal` tests are unaffected — none of them
reach a code path that calls `get_account`.)

- [ ] **Step 10: Add a new test for account-equity-unavailable during execution**

Add to `tests/test_withdrawal_execution.py`, right after
`test_execute_pending_withdrawal_retries_and_notifies_when_price_unavailable`:

```python
@pytest.mark.asyncio
async def test_execute_pending_withdrawal_retries_when_account_equity_unavailable():
    from app.withdrawal_execution import execute_pending_withdrawal
    pending_record = {
        "id": "wd-aaaa1111", "investor": "Moses", "amount": 500.0,
        "requested_at": "2026-06-21T10:00:00-05:00", "run_at": "2026-06-22T10:00:00-05:00",
    }
    with patch("app.withdrawal_execution.get_pending_withdrawal", return_value=pending_record), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.get_account", side_effect=RuntimeError("API down")), \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler, \
         patch("app.withdrawal_execution.save_investors") as mock_save, \
         patch("app.withdrawal_execution.remove_pending_withdrawal") as mock_remove, \
         patch("app.withdrawal_execution.notify_investors") as mock_notify:
        mock_notify.return_value = _async_none()
        await execute_pending_withdrawal("wd-aaaa1111")

    mock_save.assert_not_called()
    mock_remove.assert_not_called()  # stays pending — this is a retry, not a terminal outcome
    mock_scheduler.add_job.assert_called_once()
    mock_notify.assert_called_once()
```

- [ ] **Step 11: Update `tests/test_withdraw.py`**

Add a `get_account` patch (using a plain `unittest.mock.patch` with
`SimpleNamespace`, since this file doesn't already import `MagicMock`) to the
two tests that reach the investor-found path. Add this import at the top of
the file, alongside the existing imports:

```python
from types import SimpleNamespace
```

Update `test_withdraw_schedules_instead_of_writing_immediately`:

```python
def test_withdraw_schedules_instead_of_writing_immediately():
    with patch("app.withdrawal_execution.load_investors", return_value=_initial_investors()), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.get_account", return_value=SimpleNamespace(equity="2000.00")), \
         patch("app.withdrawal_execution.save_pending_withdrawal") as mock_save_pending, \
         patch("app.withdrawal_execution.scheduler") as mock_scheduler:
        response = client.post("/withdraw", json={
            "secret": TEST_SECRET,
            "investor": "Moses",
            "amount": 500.0,
        })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "scheduled"
    assert data["investor"] == "Moses"
    assert data["amount"] == 500.0
    assert data["id"].startswith("wd-")
    mock_save_pending.assert_called_once()
    mock_scheduler.add_job.assert_called_once()
```

Update `test_withdraw_returns_400_when_amount_exceeds_equity`:

```python
def test_withdraw_returns_400_when_amount_exceeds_equity():
    with patch("app.withdrawal_execution.load_investors", return_value=_initial_investors()), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.get_account", return_value=SimpleNamespace(equity="2000.00")):
        response = client.post("/withdraw", json={
            "secret": TEST_SECRET,
            "investor": "Moses",
            "amount": 50000.0,
        })
    assert response.status_code == 400
    assert "exceeds" in response.json()["error"]
```

(`_initial_investors()` in this file returns Moses with `amount=2000.0,
entry_spy=707.0` — `equity="2000.00"` is the same "equity equals total
deposited" trick used in Step 9.
`test_withdraw_returns_400_when_investor_not_found`,
`test_withdraw_rejects_zero_amount`, `test_withdraw_rejects_wrong_secret`, and
`test_withdraw_rejects_malformed_json` are unaffected.)

- [ ] **Step 12: Update `tests/test_discord_commands.py` withdrawal tests**

Add a `from types import SimpleNamespace` import near the top of the file
(alongside the existing `unittest.mock` import), then update two tests:

```python
@pytest.mark.asyncio
async def test_handle_withdraw_schedules_instead_of_writing_immediately():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=2000.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.get_account", return_value=SimpleNamespace(equity="2000.00")), \
         patch("app.withdrawal_execution.save_investors") as mock_save, \
         patch("app.withdrawal_execution.scheduler"), \
         patch("app.withdrawal_execution.save_pending_withdrawal"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    mock_save.assert_not_called()  # investors.json is NOT written yet
    msg = mock_edit.call_args[0][1]
    assert "500" in msg
    assert "Moses" in msg
    assert "cancel-withdrawal" in msg


@pytest.mark.asyncio
async def test_handle_withdraw_exceeds_total_reports_error_without_scheduling():
    from app.investors import Investor, Deposit
    inv = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=707.0, date="2026-05-09")
    ])

    with patch("app.withdrawal_execution.load_investors", return_value=[inv]), \
         patch("app.withdrawal_execution.get_latest_price", return_value=741.20), \
         patch("app.withdrawal_execution.get_account", return_value=SimpleNamespace(equity="300.00")), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_withdraw
        await handle_withdraw("Moses", 500.0, "test-token")

    msg = mock_edit.call_args[0][1]
    assert "exceeds" in msg
```

(`test_handle_withdraw_investor_not_found` is unaffected.)

- [ ] **Step 13: Run the full withdrawal-related test suite**

Run: `py -m pytest tests/test_investors.py tests/test_withdraw.py tests/test_withdrawal_execution.py tests/test_discord_commands.py -v`
Expected: all pass.

- [ ] **Step 14: Commit**

```bash
git add app/investors.py app/withdrawal_execution.py tests/test_investors.py tests/test_withdraw.py tests/test_withdrawal_execution.py tests/test_discord_commands.py
git commit -m "fix: cap withdrawals and price proceeds on real account equity, not raw SPY price"
```

---

### Task 4: Real NAV pricing for new deposits

**Files:**
- Modify: `app/discord_commands.py` (`handle_deposit`, imports)
- Modify: `app/main.py:697-774` (`POST /deposit`)
- Test: `tests/test_discord_commands.py`, `tests/test_deposit.py`

**Interfaces:**
- Consumes: `compute_nav_per_unit(investors, real_total_equity) -> float` (Task 1)
- Produces: no new public interface — this is the last consumer of `compute_nav_per_unit` in this plan.

- [ ] **Step 1: Update `app/discord_commands.py` imports**

Change:

```python
from app.investors import (
    Deposit,
    Investor,
    load_investors,
    save_investors,
    investors_lock,
)
```

to:

```python
from app.investors import (
    Deposit,
    Investor,
    compute_nav_per_unit,
    load_investors,
    save_investors,
    investors_lock,
)
```

- [ ] **Step 2: Update `handle_deposit` in `app/discord_commands.py`**

Replace the whole function:

```python
async def handle_deposit(
    investor_name: str,
    amount: float,
    spy_price: Optional[float],
    token: str,
) -> None:
    if spy_price is not None and spy_price <= 0:
        await _edit_original(token, "❌ SPY price must be positive")
        return

    manual_override = spy_price is not None
    if not manual_override:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            await _edit_original(token, "❌ Could not fetch SPY price — provide spy_price manually")
            return

    # Capture result inside lock, await Discord call outside to avoid deadlock
    is_new = False
    async with investors_lock:
        investors = load_investors()
        match = next((inv for inv in investors if inv.name.lower() == investor_name.lower()), None)
        if match is None:
            match = Investor(name=investor_name, deposits=[])
            investors.append(match)
            is_new = True

        if manual_override:
            entry_price = spy_price
        else:
            # match.deposits has NOT had the new deposit appended yet at this point,
            # so this sum is exactly "all units outstanding before this deposit" --
            # including match's own prior deposits if they're an existing investor.
            total_existing_units = sum(
                d.amount / d.entry_spy for inv in investors for d in inv.deposits if d.entry_spy
            )
            if total_existing_units <= 0:
                # Bootstrap case — no real performance to benchmark against yet.
                entry_price = spy_price
            else:
                account = get_account()
                real_total_equity = float(account.equity)
                entry_price = compute_nav_per_unit(investors, real_total_equity)

        match.deposits.append(Deposit(amount=amount, entry_spy=entry_price, date=date.today().isoformat()))
        save_investors(investors)

    status = "🆕 New investor added" if is_new else "✅ Deposit recorded"
    await _edit_original(
        token,
        f"{status} — {match.name}\n${amount:,.2f} @ NAV ${entry_price:,.2f}/unit (SPY ${spy_price:,.2f})",
    )
```

- [ ] **Step 3: Run existing `handle_deposit` tests to verify they still pass unmodified**

Run: `py -m pytest tests/test_discord_commands.py -v -k handle_deposit`
Expected: `2 passed` — `test_handle_deposit_success` (fake investor has `deposits = []`
and is the only investor, so `total_existing_units == 0` → bootstrap fallback,
`get_account` never called) and `test_handle_deposit_investor_not_found`
(empty investor list, same bootstrap path) both still pass with zero changes,
since neither has any pre-existing units outstanding.

- [ ] **Step 4: Add a new test for NAV-priced deposits in `discord_commands.py`**

Add to `tests/test_discord_commands.py`, right after `test_handle_deposit_investor_not_found`:

```python
@pytest.mark.asyncio
async def test_handle_deposit_prices_at_real_nav_when_units_outstanding():
    from app.investors import Investor, Deposit
    from types import SimpleNamespace
    existing = Investor(name="Moses", deposits=[
        Deposit(amount=300.0, entry_spy=600.0, date="2026-01-01")  # 0.5 units
    ])

    with patch("app.discord_commands.load_investors", return_value=[existing]), \
         patch("app.discord_commands.get_latest_price", return_value=580.0), \
         patch("app.discord_commands.get_account", return_value=SimpleNamespace(equity="350.00")), \
         patch("app.discord_commands.save_investors"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_deposit
        await handle_deposit("David", 500.0, None, "test-token")

    msg = mock_edit.call_args[0][1]
    # nav_per_unit = 350.00 / 0.5 = 700.0 -- the new deposit is priced at real
    # NAV (700.00), not the raw SPY price (580.00) shown alongside it.
    assert "700.00" in msg
    assert "580.00" in msg


@pytest.mark.asyncio
async def test_handle_deposit_falls_back_to_spy_price_with_no_units_outstanding():
    from types import SimpleNamespace

    with patch("app.discord_commands.load_investors", return_value=[]), \
         patch("app.discord_commands.get_latest_price", return_value=741.20), \
         patch("app.discord_commands.get_account") as mock_account, \
         patch("app.discord_commands.save_investors"), \
         patch("app.discord_commands._edit_original", new_callable=AsyncMock) as mock_edit:
        from app.discord_commands import handle_deposit
        await handle_deposit("Moses", 2000.0, None, "test-token")

    mock_account.assert_not_called()
    msg = mock_edit.call_args[0][1]
    assert "741.20" in msg
```

- [ ] **Step 5: Run the full `discord_commands.py` deposit test slice**

Run: `py -m pytest tests/test_discord_commands.py -v -k handle_deposit`
Expected: `4 passed`

- [ ] **Step 6: Update `POST /deposit` in `app/main.py`**

Replace lines 744-750:

```python
    spy_price = req.spy_price
    if spy_price is None:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            raise HTTPException(status_code=502, detail="Could not fetch current SPY price from Alpaca.")

    new_deposit = Deposit(amount=req.amount, entry_spy=spy_price, date=date.today().isoformat())
```

with:

```python
    manual_override = req.spy_price is not None
    spy_price = req.spy_price
    if not manual_override:
        spy_price = get_latest_price("SPY")
        if spy_price is None:
            raise HTTPException(status_code=502, detail="Could not fetch current SPY price from Alpaca.")
```

Then replace the block from `async with investors_lock:` through the end of
the function (lines 752-774):

```python
    async with investors_lock:
        investors = load_investors()
        match = next(
            (inv for inv in investors if inv.name.lower() == req.investor.lower()),
            None,
        )
        if match is None:
            match = Investor(name=req.investor, deposits=[])
            investors.append(match)

        if manual_override:
            entry_price = spy_price
        else:
            # match.deposits has NOT had the new deposit appended yet at this point,
            # so this sum is exactly "all units outstanding before this deposit" --
            # including match's own prior deposits if they're an existing investor.
            total_existing_units = sum(
                d.amount / d.entry_spy for inv in investors for d in inv.deposits if d.entry_spy
            )
            if total_existing_units <= 0:
                entry_price = spy_price
            else:
                account = get_account()
                real_total_equity = float(account.equity)
                entry_price = compute_nav_per_unit(investors, real_total_equity)

        match.deposits.append(Deposit(amount=req.amount, entry_spy=entry_price, date=date.today().isoformat()))
        save_investors(investors)

    from app.backup import push_backup
    _fire(push_backup())

    return {
        "investor": match.name,
        "deposits": [
            {"amount": d.amount, "entry_spy": d.entry_spy, "date": d.date}
            for d in match.deposits
        ],
    }
```

Add `compute_nav_per_unit` to the existing `app.investors` import block in
`app/main.py` (lines 36-45):

```python
from app.investors import (
    Deposit,
    Investor,
    Withdrawal,
    compute_nav_per_unit,
    compute_withdrawal_lots,
    format_withdrawal_message,
    load_investors,
    save_investors,
    investors_lock,
)
```

(`get_account` is already imported at line 69.)

- [ ] **Step 7: Update `tests/test_deposit.py`**

Replace the fixture and three tests. First, the fixture (give it a round
`entry_spy` so the arithmetic in updated assertions is exact):

```python
def _initial_investors():
    return [
        Investor(name="Moses", deposits=[Deposit(amount=300.0, entry_spy=600.0, date="2026-05-09")])
    ]
```

Add this import near the top of the file, alongside the existing imports:

```python
from types import SimpleNamespace
```

Update `test_deposit_appends_to_existing_investor`:

```python
def test_deposit_appends_to_existing_investor():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price", return_value=580.0):
                with patch("app.main.get_account", return_value=SimpleNamespace(equity="350.00")):
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
    # Moses already has 300/600 = 0.5 units; nav_per_unit = 350.00/0.5 = 700.0,
    # not the raw SPY price of 580.0.
    assert data["deposits"][1]["entry_spy"] == 700.0
```

Update `test_deposit_uses_provided_spy_price_and_skips_alpaca_call` (manual
override must also skip the account-equity fetch):

```python
def test_deposit_uses_provided_spy_price_and_skips_alpaca_call():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price") as mock_price:
                with patch("app.main.get_account") as mock_account:
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "Moses",
                        "amount": 500.0,
                        "spy_price": 595.0,
                    })
    assert response.status_code == 200
    mock_price.assert_not_called()
    mock_account.assert_not_called()
    assert response.json()["deposits"][1]["entry_spy"] == 595.0
```

Update `test_deposit_creates_new_investor_when_name_not_found` (Moses still
has 0.5 units outstanding even though Alice is the new investor, so NAV
pricing applies to Alice's first deposit too):

```python
def test_deposit_creates_new_investor_when_name_not_found():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price", return_value=580.0):
                with patch("app.main.get_account", return_value=SimpleNamespace(equity="350.00")):
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
    assert data["deposits"][0]["entry_spy"] == 700.0
```

Update `test_deposit_matches_investor_name_case_insensitively` (no assertion
change needed, just add the `get_account` patch so it doesn't hit a real
network call):

```python
def test_deposit_matches_investor_name_case_insensitively():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price", return_value=580.0):
                with patch("app.main.get_account", return_value=SimpleNamespace(equity="350.00")):
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "moses",
                        "amount": 200.0,
                    })
    assert response.status_code == 200
    assert response.json()["investor"] == "Moses"
```

Update `test_deposit_saves_to_disk` (same — just add the patch):

```python
def test_deposit_saves_to_disk():
    with patch("app.main.load_investors", return_value=_initial_investors()):
        with patch("app.main.save_investors") as mock_save:
            with patch("app.main.get_latest_price", return_value=580.0):
                with patch("app.main.get_account", return_value=SimpleNamespace(equity="350.00")):
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "Moses",
                        "amount": 500.0,
                    })
    assert response.status_code == 200
    mock_save.assert_called_once()
```

(`test_deposit_rejects_wrong_secret`, `test_deposit_rejects_malformed_json`,
`test_deposit_returns_502_when_spy_price_unavailable`, and
`test_deposit_rejects_zero_amount` are unaffected.)

- [ ] **Step 8: Add a bootstrap-case test for `POST /deposit`**

Add to `tests/test_deposit.py`, right after `test_deposit_saves_to_disk`:

```python
def test_deposit_falls_back_to_spy_price_when_no_units_outstanding():
    with patch("app.main.load_investors", return_value=[]):
        with patch("app.main.save_investors"):
            with patch("app.main.get_latest_price", return_value=580.0):
                with patch("app.main.get_account") as mock_account:
                    response = client.post("/deposit", json={
                        "secret": TEST_SECRET,
                        "investor": "Moses",
                        "amount": 300.0,
                    })
    assert response.status_code == 200
    assert response.json()["deposits"][0]["entry_spy"] == 580.0
    mock_account.assert_not_called()
```

- [ ] **Step 9: Run the full deposit test suite**

Run: `py -m pytest tests/test_deposit.py tests/test_discord_commands.py -v`
Expected: all pass.

- [ ] **Step 10: Run the entire project test suite**

Run: `py -m pytest tests/ -v`
Expected: all pass, no regressions outside what this plan touched. (The
pre-existing `test_public_stats.py` collection failure on this machine's
Python 3.9.7, noted in earlier work on this repo, is unrelated and expected
to still be present.)

- [ ] **Step 11: Commit**

```bash
git add app/discord_commands.py app/main.py tests/test_discord_commands.py tests/test_deposit.py
git commit -m "feat: price new deposits at real NAV-per-unit instead of raw SPY price"
```
