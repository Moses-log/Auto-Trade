# Delayed Withdrawal Approval — Design Spec

## Goal

`/withdraw` is currently fully synchronous: it validates, writes the withdrawal to `investors.json`, and saves, all within a single Discord interaction. Authorization is a single Discord user ID check (`main.py:307`, `settings.discord_your_user_id`). If that one Discord account is ever compromised (phishing, stolen session token), an attacker can drain any investor's recorded balance immediately, with only a Discord message as the trail.

This spec adds a delay-and-cancel window to `/withdraw` so the legitimate operator has a chance to notice and stop a fraudulent request before the investor ledger is actually changed — even if the attacker is using the operator's own compromised Discord session, since the operator may still have working access elsewhere (another device/session) during the delay window.

## Non-goals

- `/deposit`, `/close`, `/rebalance`, `/rh_deposit` are untouched. `/close` in particular is the emergency-stop command — delaying it would be actively harmful.
- No second notification channel (Telegram) is added. Alerts remain Discord-only.
- No multi-user authorization. `discord_your_user_id` remains a single value; this feature adds friction to the one existing authorized user, not a second approver.
- No change to how `investors.json` withdrawals are computed (FIFO lot math, SPY pricing) — only *when* the write happens.

## Current Behavior (for reference)

`app/discord_commands.py:82-127` (`handle_withdraw`):
1. Fetch current SPY price.
2. Acquire `investors_lock`, compute withdrawal lots via `compute_withdrawal_lots()`.
3. Append a `Withdrawal` record to the investor and call `save_investors()` — permanent write.
4. Release lock.
5. Fire `notify_investors()` (Discord webhook) and `push_backup()` in the background.
6. Edit the original Discord interaction response with a confirmation.

## New Behavior

### 1. Pending withdrawal record

Mirrors the existing pending-order pattern in `app/pending_orders.py` / `pending_orders.json`, which already stores a `run_at` ISO timestamp and is rescheduled on startup via `scheduler.py:179-233` (`reschedule_pending_orders()`).

New file `pending_withdrawals.json` (same shape of concern as `pending_orders.json`, kept separate since it's a different domain — investor ledger vs. broker orders):

```json
{
  "pending": [
    {
      "id": "wd-<uuid4>",
      "investor": "string",
      "amount": float,
      "requested_at": "ISO datetime",
      "run_at": "ISO datetime",
      "status": "pending"
    }
  ]
}
```

`id` is a short UUID, used by `/cancel-withdrawal`.

### 2. `/withdraw` flow changes

`handle_withdraw()` changes to:
1. Same validation as today (investor exists, sufficient balance, fetch SPY price) — but **does not** call `compute_withdrawal_lots()` / write to `investors.json` yet, since the SPY price and lot math should be computed at execution time (`run_at`), not request time, to reflect the actual market state at withdrawal.
2. Generate a pending withdrawal record with `run_at = now + WITHDRAWAL_DELAY_HOURS` (new setting in `app/config.py`, default `24`).
3. Persist via a new `save_pending_withdrawal()` in a new `app/pending_withdrawals.py` (parallel structure to `app/pending_orders.py`).
4. Schedule an APScheduler date-trigger job (same `scheduler.add_job(..., "date", run_date=run_dt, ...)` pattern used at `main.py:504-508`) that calls a new `execute_pending_withdrawal(id)`.
5. Post a Discord message: `Withdrawal of $<amount> for <investor> scheduled for <run_at, formatted CST>. Run /cancel-withdrawal id=<id> to stop it.`
6. Call `notify_investors()` with the same "scheduled" message (so the investor-facing channel sees it was requested, consistent with today's behavior of always notifying).

### 3. `execute_pending_withdrawal(id)`

Runs when the APScheduler job fires (no cancellation happened in the interim):
1. Re-fetch current SPY price (at execution time, not request time).
2. Run the existing `compute_withdrawal_lots()` + `investors_lock`-guarded append + `save_investors()` — i.e., exactly what `handle_withdraw()` does today, just deferred.
3. Remove the record from `pending_withdrawals.json` and append a `{id, investor, amount, requested_at, run_at, status: "executed"}` entry to `withdrawal_audit.json` (the same audit log used for cancellations — see below).
4. Call `notify_investors()` with the final confirmation (amount, SPY exit price, proceeds) and `push_backup()`.

### 4. `/cancel-withdrawal id=<id>` (new command)

Registered in `dispatch_command()` (`discord_commands.py:419-477`), same single-user auth as every other command (no new auth path).
1. Look up the pending record by `id`. If not found or already executed, reply with an error.
2. Remove the APScheduler job (`scheduler.remove_job(...)`) and the pending record from `pending_withdrawals.json`.
3. Append a `{id, investor, amount, requested_at, run_at, status: "canceled", canceled_at}` entry to `withdrawal_audit.json` — the same append-only audit log used for executed and failed withdrawals (Section 3), so every pending withdrawal's outcome (executed, canceled, or failed) ends up in one place.
4. Confirm via Discord ("Withdrawal wd-xxxx for X canceled.").

### 5. Visibility: `/pending-withdrawals` (new read-only command)

Lists all currently-pending withdrawals (id, investor, amount, scheduled time) so the operator can audit state without opening JSON files. Same auth as other commands; no mutation.

### 6. Startup rescheduling

Extend the existing `reschedule_pending_orders()`-style startup hook (or add a sibling `reschedule_pending_withdrawals()`, called alongside it in `main.py`) so pending withdrawals survive an app restart during the delay window — same mechanism already proven for `pending_orders.json`.

## Data Model Summary

| File | New/Modified | Purpose |
|---|---|---|
| `pending_withdrawals.json` | New | Withdrawals awaiting the delay window |
| `withdrawal_audit.json` | New | Append-only record of every pending withdrawal's terminal outcome (executed, canceled, or failed) |
| `investors.json` | Unchanged schema | Write deferred to execution time instead of request time |
| `app/config.py` | Modified | New `withdrawal_delay_hours: int = 24` setting |

## Error Handling

- If the scheduled job fires but the investor's balance is no longer sufficient (e.g., a loss occurred during the delay window), `execute_pending_withdrawal` should fail gracefully: remove the pending record, append a `{..., status: "failed", reason}` entry to `withdrawal_audit.json`, notify via Discord with the reason, and NOT partially write to `investors.json`. This is the same validation `compute_withdrawal_lots()` already does today — re-run at execution time, not skipped.
- If `/cancel-withdrawal` is called with an `id` that already executed (race: job fired moments before cancel arrived), reply clearly that it already executed rather than silently failing.

## Testing

- Unit tests for `app/pending_withdrawals.py` (save/load/remove), mirroring whatever test coverage `app/pending_orders.py` has today (to be confirmed at plan time).
- Unit test for `/cancel-withdrawal` removing both the scheduler job and the pending record.
- Integration-style test: request a withdrawal with `WITHDRAWAL_DELAY_HOURS=0` (or a manually-fired job in tests) and confirm the resulting `investors.json` state matches what today's synchronous `/withdraw` would have produced.
- Test the restart-survival path: persist a pending withdrawal, simulate startup rescheduling, confirm the job is re-added.
