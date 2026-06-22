# Real NAV-Per-Unit Pricing — Design Spec
**Date:** 2026-06-22
**Project:** Moses-log/Auto-Trade

---

## Problem

The Discord investor breakdown (`📊 Investor Breakdown`) reports a `Total Portfolio`
figure that doesn't match the real Alpaca account equity. Per the original
investor-tracking spec (`2026-05-09-investor-tracking-design.md`), this was
intentional: every dollar figure is computed by valuing each investor's SPY-unit
holdings (`units = deposit_amount / SPY_price_at_deposit`) at *today's raw SPY
market price* — a synthetic "what if you'd just bought and held SPY" benchmark,
deliberately decoupled from the fund's real trading P&L.

That benchmark has two consequences once the fund's actual trading diverges from
raw SPY price movement (which it will, since the bot actively trades SPY rather
than buy-and-hold):

1. **Display:** the Discord report shows fictional equity/P&L numbers instead of
   investors' real stake in real money.
2. **Withdrawals:** `compute_withdrawal_lots()` uses the same raw-SPY-price model
   to cap how much an investor may withdraw. If the fund's real equity is lower
   than the synthetic benchmark implies, an investor could be allowed to withdraw
   more real cash than their true proportional share of the fund — at the expense
   of remaining investors.

This spec replaces the raw-SPY-price valuation with a real NAV-per-unit, computed
from the fund's actual Alpaca account equity, everywhere money is valued: the
breakdown report, withdrawal caps, withdrawal proceeds, and (going forward) new
deposit pricing.

---

## Core Concept: NAV-Per-Unit

```
nav_per_unit = real_total_equity / total_units_outstanding
```

Where `real_total_equity` comes from `get_account().equity` (the real Alpaca
account, fetched live) and `total_units_outstanding` is the sum of every
investor's `_net_units()` — **unchanged**, still `deposits minus withdrawn units`.
Units themselves are not redefined; only the price used to value them changes
from "today's raw SPY price" to "today's real fund NAV per unit."

If `total_units_outstanding` is 0 (no deposits yet, or every investor has fully
withdrawn), `nav_per_unit` is undefined — callers fall back to raw SPY price
(see "Bootstrap case" below).

**New function in `app/investors.py`:**

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

`entry_spy` (the price recorded per deposit) and the FIFO tax-lot logic that
depends on it are **not redefined or migrated**. Cost basis math
(`consume_units * entry_spy`) is self-consistent regardless of what `entry_spy`
conceptually represents at the time it was recorded — it always recovers the
original dollars invested for those units. Existing historical deposits keep
their recorded raw-SPY-price `entry_spy` values unchanged.

---

## 1. Investor Breakdown Report (`app/investors.py`, `app/pnl.py`)

`compute_breakdown()` gains a required parameter and uses `nav_per_unit` instead
of `spy_price` for every dollar figure:

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
        # ... gross_deposited / withdrawn_basis / net_cost_basis / dollar_pnl /
        #     pct_pnl unchanged in structure, just now driven by the real
        #     current_equity above instead of net_units * spy_price
        ...
    # total_portfolio, portfolio_share, total_deposited, overall_dollar_pnl,
    # overall_pct_pnl computed exactly as today, from the new current_equity values
```

`spy_price` is kept as a parameter purely so `format_discord_message()` can still
show the `SPY: $X` line for market context — it no longer drives any dollar math.

**`InvestorBreakdown.total_portfolio` will now equal (modulo float rounding)
`real_total_equity` exactly** — this is the fix for the reported symptom.

**`send_investor_report()` in `app/pnl.py`** fetches `get_account()` alongside
the existing `get_latest_price("SPY")` call. If `get_account()` fails or returns
no usable equity, the report is skipped with a warning logged — same failure
posture as the existing "SPY price unavailable" check immediately above it.

---

## 2. Withdrawal Cap and Proceeds (`app/investors.py`, `app/withdrawal_execution.py`)

`compute_withdrawal_lots()` swaps its `current_spy` parameter for `nav_per_unit`
for all valuation math. `entry_spy`-based cost basis and holding-period/tax
classification are untouched.

```python
def compute_withdrawal_lots(
    investor: Investor,
    withdraw_amount: float,
    nav_per_unit: float,
) -> tuple[list[dict], float]:
    available_equity = _net_units(investor) * nav_per_unit
    if withdraw_amount > available_equity + 0.005:
        raise ValueError(
            f"Withdrawal ${withdraw_amount:,.2f} exceeds available equity "
            f"${available_equity:,.2f}"
        )
    units_to_redeem = withdraw_amount / nav_per_unit
    # FIFO walk over investor.deposits is unchanged structurally; per lot:
    #   lot_cost     = consume * d.entry_spy      (unchanged — original cost basis)
    #   lot_proceeds = consume * nav_per_unit      (changed — real proceeds, not SPY-priced)
    #   lot_gain     = lot_proceeds - lot_cost
    # sum(lot_proceeds) == units_to_redeem * nav_per_unit == withdraw_amount exactly
    ...
    return lots, units_to_redeem
```

**`format_withdrawal_message()`** gains a `nav_per_unit` parameter (in addition
to the existing `current_spy`, which stays for the `SPY @ $X` header line only).
`remaining_equity = remaining_units * nav_per_unit` instead of `* current_spy` —
the "Remaining Position" equity shown after a partial withdrawal must reflect
real value, not the SPY benchmark.

**`schedule_withdrawal()` and `execute_pending_withdrawal()`** in
`withdrawal_execution.py` both already `load_investors()` (the full list, needed
for `compute_nav_per_unit`'s total-units sum). Each now also calls `get_account()`
alongside its existing `get_latest_price("SPY")` call, and computes
`nav_per_unit = compute_nav_per_unit(investors, float(account.equity))` before
calling `compute_withdrawal_lots()`.

- In `schedule_withdrawal()`: if `get_account()` fails, raise
  `WithdrawalValidationError` immediately — same posture as the existing
  SPY-price-unavailable check.
- In `execute_pending_withdrawal()`: if `get_account()` fails, fold it into the
  existing retry-in-15-minutes branch that already handles SPY-price
  unavailability (treat "can't get market/account data" as one condition for
  retry purposes, not two separate code paths).

If `nav_per_unit` comes back `0.0` (bootstrap case — no units outstanding), the
investor has nothing to withdraw; `compute_withdrawal_lots` will naturally reject
any positive `withdraw_amount` since `available_equity` is `0`.

---

## 3. New Deposit Pricing (`app/discord_commands.py`, `app/main.py`)

Today, `entry_spy` for a new deposit is the raw SPY price at deposit time
(auto-fetched, or manually overridden). Going forward, the **auto-fetched path**
prices new deposits at real NAV-per-unit instead, so a new depositor buys in at
the fund's true current value rather than a benchmark that may have drifted from
the fund's real performance.

**Manual override is preserved exactly as today:** if the caller explicitly
passes `spy_price`, that value is used directly as `entry_spy` — no NAV
computation happens. This preserves the existing ability to backfill historical
deposits at a specific known price (as the original 3 investors' inception
deposits were recorded).

**Auto-fetch path** (`spy_price` omitted): fetch live SPY price as today (now
display-only), fetch `get_account().equity`, and compute
`nav_per_unit = compute_nav_per_unit(investors, equity)` using the investor list
*as it stands before this deposit is appended*. Use `nav_per_unit` as `entry_spy`
for the new `Deposit` record.

**Bootstrap case:** if `compute_nav_per_unit` returns `0.0` (zero units
outstanding — first-ever deposit, or every existing investor has fully
withdrawn), there is no real performance to benchmark against yet. Fall back to
the raw SPY price, exactly like today.

Both `handle_deposit()` (`discord_commands.py`) and the `POST /deposit` handler
(`main.py`) get the same change, applied identically since they duplicate this
logic today.

**Confirmation message** changes from `"$X @ SPY $Y"` to `"$X @ NAV $Y/unit (SPY
$Z)"` so the depositor can see both the price they were actually credited at and
the raw market reference price, e.g.:

```
✅ Deposit recorded — Hoang Lieu
$1,500.00 @ NAV $731.42/unit (SPY $744.27)
```

---

## Out of Scope

- **Tax-lot cost basis and holding-period classification** — entirely untouched.
  `entry_spy` keeps meaning "the price this lot's units were recorded at,"
  whatever that price's source was at the time. No historical data migration.
- **`compute_time_weighted_capital()` / `app/tax.py`** — these already operate
  purely on deposit/withdrawal dollar amounts, not SPY price or units. Not
  affected by this change.
- **Backfilling NAV history** — no attempt to retroactively recompute what
  `nav_per_unit` "should have been" on past dates. The fix applies going forward
  from whenever it ships.

---

## Files Changed

| File | Change |
|------|--------|
| `app/investors.py` | New `compute_nav_per_unit()`. `compute_breakdown()` takes `real_total_equity`, uses NAV instead of raw SPY price. `compute_withdrawal_lots()` takes `nav_per_unit` instead of `current_spy`. `format_withdrawal_message()` takes an added `nav_per_unit` param. |
| `app/pnl.py` | `send_investor_report()` fetches `get_account()`, passes `real_total_equity` to `compute_breakdown()`; skips report with a warning if unavailable. |
| `app/withdrawal_execution.py` | `schedule_withdrawal()` and `execute_pending_withdrawal()` fetch `get_account()`, compute `nav_per_unit`, pass it to `compute_withdrawal_lots()`/`format_withdrawal_message()`. `get_account()` failure handling mirrors existing SPY-price-unavailable handling in each function. |
| `app/discord_commands.py` | `handle_deposit()` auto-fetch path prices new deposits via `compute_nav_per_unit()`, falling back to raw SPY price when no units are outstanding. Confirmation message updated. |
| `app/main.py` | `POST /deposit` handler gets the identical auto-fetch pricing change. |

## Tests Affected

Existing tests that call `compute_breakdown()`, `compute_withdrawal_lots()`, or
`format_withdrawal_message()` directly (`tests/test_investors.py`) need their
call sites updated to the new signatures. Withdrawal-flow tests that patch
`get_latest_price` (`tests/test_withdraw.py`, `tests/test_withdrawal_execution.py`,
`tests/test_discord_commands.py`) need an added patch for `get_account()`
returning a fake account object with a `.equity` attribute. New tests are needed
for: `compute_nav_per_unit()` itself (normal case, zero-units bootstrap case),
the bootstrap fallback path in deposit handling, and a withdrawal-cap test that
demonstrates the real-equity cap differs from (and now overrides) what the old
raw-SPY-price cap would have allowed.
