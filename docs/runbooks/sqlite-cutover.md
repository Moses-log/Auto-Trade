# Investor Ledger SQLite — Cutover & Rollback Runbook

## Preconditions
- Branch `feature/investor-ledger-sqlite` deployed to Render with `USE_SQLITE=false`.
- Confirm the app is healthy (`GET /healthz`) and behaving normally on JSON.

## Cutover
1. Take a manual Gist backup: `POST /run-backup {"secret": "<WEBHOOK_SECRET>"}`.
2. Open the Render shell and run the migration (flag still off for the read side):
   `python -m scripts.migrate_to_sqlite`
3. Confirm the final line is `✅ VERIFIED — investors=… deposits=… withdrawals=…`.
   If it prints `❌ VERIFICATION FAILED`, STOP — do not flip the flag; the JSON
   files are untouched and the app is still running on them.
4. Set Render env `USE_SQLITE=true` and redeploy.
5. Smoke test: `GET /public-stats`, run `/run-report {"report":"investors"}` and
   confirm the Investor Tracker Discord message looks identical to before.

## Rollback (instant, lossless)
1. Set Render env `USE_SQLITE=false` and redeploy.
2. The app resumes on the JSON files, which the SQLite path kept current on every
   write. The frozen pre-migration copy is in `/data/backup_pre_sqlite/` if needed.

## Notes
- `/data/kimi.db` is the source of truth while `USE_SQLITE=true`; the JSON files are
  regenerated after every committed write and are safe to read but not to hand-edit.
- Backups now include `kimi.db.base64` in the Gist alongside the JSON snapshots.

### Restore from backup
- The JSON files in the Gist are the primary recovery source — they are always
  current (regenerated after every committed write), so restore from them first.
- To restore the binary `kimi.db` as well: fetch the Gist, then decode the base64
  entry back into the data dir:
  `base64 -d kimi.db.base64 > /data/kimi.db`
