# 🧯 Guarded repairs: `tools/`

Prod history changes (migrations, price or benchmark repairs, ledger corrections) go through **one rehearsed, guarded transaction**. Bulk changes are never typed straight into the tables.

```
prod ──read-only──▶ snapshot ──▶ replica ≡ prod ──▶ migrations + fix + backfill plan ──▶ post state
                                                                                              │
prod ◀── apply_deploy.sh ◀── deploy.sql = BEGIN · guard(pre) · SQL · guard(post) · COMMIT ◀───┘
```

`deploy.sql` commits only if two things hold:

1. Before the change, prod still equals the snapshot the plan was built from.
2. After the change, prod equals the rehearsal.

Otherwise a guard raises and Postgres rolls the whole transaction back. A guard compares one fingerprint per table: the row count plus the MD5 of every row's value columns in a fixed order.

| File | Role |
|---|---|
| `rebuild.sh` | Builds the plan and rehearses it. **Read-only on prod.** |
| `apply_deploy.sh` | The only step that writes to prod. |
| `rehearsal_up.sh` | Starts a fresh local Postgres replica from a snapshot (bound to 127.0.0.1). |
| `snapshot.py` | Takes a CSV snapshot of every public table (Management API, read-only). |
| `fingerprint.py` | Computes the table fingerprints and builds the guarded `deploy.sql`. |
| `sb_query.py` | Runs SQL through `supabase db query --linked`. No database password needed. |
| `ledger_fix.example.sql` | Template for a one-shot ledger correction. |
| `dashboard_role.sql` | Optional least-privilege login for the public app: SELECT only, return columns only on `portfolio_performance`, read-only by default. Idempotent; it refuses to run until the password placeholder is replaced. |

## One-time setup

Requirements: Docker running, the [Supabase CLI](https://supabase.com/docs/guides/cli) (`brew install supabase/tap/supabase`) and Python 3.10+.

```zsh
cd portfolio_dashboard
supabase login                                                   # personal access token
supabase init --workdir .repair/supabase                         # answer No to the editor prompts
supabase link --project-ref <project-ref> --workdir .repair/supabase   # DB password: leave blank
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export PYTHON=$PWD/.venv/bin/python
```

`.repair/` is git-ignored. The CLI link, the market-data cache and every run directory live there.

## 1. Rebuild (read-only on prod)

```zsh
tools/rebuild.sh                                  # → .repair/run_<UTC stamp>/
tools/rebuild.sh --ledger-fix .repair/fix.sql     # with a correction (see ledger_fix.example.sql)
```

A run takes about 2 minutes. The first one takes longer because it downloads B3's annual COTAHIST files into `.repair/cache`.

| Step | Check | What it proves |
|---|---|---|
| 1 | Detect the prod schema (`legacy` = before `sql/001`, `current` = after), take a snapshot and fingerprint prod | The plan starts from a known, hashed prod state. |
| 2 | Replica fingerprint = prod fingerprint | The snapshot is complete and loads faithfully. |
| 3 | Run migrations, the fix and `backfill_history.py --emit-sql` on the replica | The repair is computed against an exact copy of prod. |
| 4 | Build `deploy.sql` | Migrations, the fix and the repair sit in one transaction between the pre and post guards. |
| 5 | Apply `deploy.sql` to a second fresh replica; its fingerprint must equal step 3's | The deploy is self-contained and deterministic. |
| 6 | Run the daily TWR job on the rehearsed replica; it must write 0 rows | The next daily run agrees with the repaired state, so there is no drift and no revision alert. |
| 7a | Apply `deploy.sql` a second time; it must fail and change nothing | A double apply cannot do harm. |
| 7b | Insert a price row without `source`; it must be rejected | The database enforces provenance. |

When every check passes, the run directory gets a `REHEARSED_OK` manifest and the script prints the apply command. The manifest records the rehearsal time, the prod schema, whether the data is unchanged (no-op), the fix used and the SHA-256 of `deploy.sql`.

Review these files before you apply:

- `backfill.log`: dates, counts and percentages only, so it is safe to share.
- `recon.md`: nominal reconciliation and the IRPF monthly view. **Keep it local.**

### Ledger fixes

- Use plain SQL only. Prod runs through the Management API, so `rebuild.sh` refuses files with psql meta-commands such as `\copy`.
- Make every statement assert the exact number of rows it touches (see the template), so that a stale or mistyped fix aborts the deploy.
- Keep the file in `.repair/`, because it contains ledger data.
- `rebuild.sh` first copies all seven tables to `backup.<table>_pre_fix_<stamp>`, inside the same transaction.

## 2. Apply (the one prod write)

```zsh
tools/apply_deploy.sh .repair/run_<stamp> --yes
```

The script refuses to run without `--yes`, without `REHEARSED_OK`, or if `deploy.sql` changed after the rehearsal. After the commit it fingerprints prod again and checks that the result equals the rehearsal (`post.csv`).

Run it outside the daily pipeline windows (07:30 and 13:30 BRT). A daily run that writes between the rebuild and the apply trips the pre-state guard. That failure is safe, but you have to rebuild.

## When something fails

- **A rebuild step fails.** Nothing touched prod. Read the log named in the error (it is in the run directory), fix the cause and rerun.
- **`pre-state guard failed` on apply.** Prod changed after the snapshot, for example through a daily run or a manual entry. Postgres rolled everything back. Run `tools/rebuild.sh` again and apply the new run.
- **`post-state guard failed` on apply.** Postgres rolled everything back, but the same SQL produced a different result on prod than on the replica. That points to non-determinism or a difference between the replica and prod. Investigate before you rebuild.
- **Never edit `deploy.sql`.** Its guards hash the exact snapshot, and `apply_deploy.sh` refuses a modified file. Change the inputs (the fix file, a migration or pipeline code) and rebuild.
- **Any other apply error.** `apply_deploy.sh` fingerprints prod (read-only) and reports which case you are in:
  - prod is unchanged;
  - prod equals the rehearsal (the commit landed and only the reply was lost);
  - prod was changed by someone else.

## Rollback after a committed deploy

Each deploy leaves point-in-time copies in the `backup` schema, which the API roles cannot read:

| Copy | Taken | Tables |
|---|---|---|
| `backup.<table>_pre_001` | First run of `sql/001` | prices, benchmark, portfolio_performance, trades, dividends |
| `backup.<table>_pre_fix_<stamp>` | Just before a `--ledger-fix` | All seven public tables |
| `backup.<table>_pre_backfill_<stamp>` | Just before the repair statements | prices, benchmark, corporate_actions, portfolio_performance |

List the copies:

```sql
SELECT table_name FROM information_schema.tables WHERE table_schema = 'backup' ORDER BY table_name;
```

Restore a child table in one transaction. `OVERRIDING SYSTEM VALUE` keeps the original identity ids of `prices` and `benchmark`:

```sql
BEGIN;
DELETE FROM public.prices;
INSERT INTO public.prices OVERRIDING SYSTEM VALUE
SELECT * FROM backup.prices_pre_backfill_<stamp>;
COMMIT;
```

- **Never `DELETE FROM public.assets`.** Its foreign keys are `ON DELETE CASCADE`, so the delete wipes every trade, dividend, price and corporate action that references it. Restore `assets` with `UPDATE` / `INSERT … ON CONFLICT`.
- `SELECT *` only lines up when the copy has today's columns. A `_pre_001` copy predates the provenance columns, so list the columns explicitly.
- `portfolio_performance` does not need a restore. The next daily run rebuilds it from the restored inputs, so expect a 📝 *History revised* alert.
- Drop the copies you no longer need with `DROP TABLE backup.<name>;`.

## Data hygiene

- A run directory holds the full ledger (`snapshot/`), the nominal `recon.md` and a `deploy.sql` that embeds ledger-derived values. Keep it local and delete it after the apply: `rm -rf .repair/run_<stamp>`.
- `.repair/cache` holds public market data only (B3 COTAHIST and Yahoo). Keeping it makes reruns fast and repeatable.
- The replica listens on 127.0.0.1 only and is removed when `rebuild.sh` exits. Set `KEEP_REPLICA=1` to keep it for debugging, then remove it with `docker rm -f pd-rehearsal`.
- Rehearsals blank the Telegram variables, so a rehearsal can never alert the real channel.

## Environment

| Variable | Default | Used by |
|---|---|---|
| `PYTHON` | `python3` | Every script. It needs the packages in `requirements-dev.txt`. |
| `SUPABASE_WORKDIR` | `.repair/supabase` | `sb_query.py`, `rebuild.sh`, `apply_deploy.sh` |
| `B3_CACHE_DIR` | `.repair/cache` | `rebuild.sh` |
| `KEEP_REPLICA` | `0` | `rebuild.sh` |
| `REPLICA_NAME` | `pd-rehearsal` | `rehearsal_up.sh`, `rebuild.sh` |
| `REPLICA_PORT` | `55432` | `rehearsal_up.sh`, `rebuild.sh` |
| `REPLICA_PASSWORD` | `rehearsal` (local throwaway) | `rehearsal_up.sh`, `rebuild.sh` |
| `REPLICA_IMAGE` | `postgres:17` | `rehearsal_up.sh` |
