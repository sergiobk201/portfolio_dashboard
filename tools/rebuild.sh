#!/bin/zsh
# Guarded-repair rebuild. READ-ONLY on prod:
#   fresh prod snapshot → local replica (must fingerprint-equal prod) → migrations
#   + optional ledger fix + backfill plan → guarded deploy.sql → dress rehearsal
#   on a second fresh replica → safety checks. Apply with tools/apply_deploy.sh.
#
# Usage: tools/rebuild.sh [--out DIR] [--ledger-fix FILE.sql]
# Env:   PYTHON            interpreter with requirements-dev.txt installed (python3)
#        SUPABASE_WORKDIR  directory linked with `supabase link` (.repair/supabase)
#        B3_CACHE_DIR      COTAHIST annual files + Yahoo caches (.repair/cache)
#        KEEP_REPLICA=1    leave the replica running at exit (debugging)
#        REPLICA_*         see rehearsal_up.sh
#
# The run directory holds the FULL ledger and a nominal report: never commit it.
set -euo pipefail
TOOLS=${0:A:h}
REPO=${TOOLS:h}
PY=${PYTHON:-python3}
OUT=""
FIX=""
while (( $# )); do
  case $1 in
    --out) OUT=${2:?--out needs a directory}; shift 2 ;;
    --ledger-fix) FIX=${${2:?--ledger-fix needs a file}:A}; shift 2 ;;
    -h|--help) sed -n '2,14p' $0; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -z $FIX || -f $FIX ]] || { echo "ledger fix not found: $FIX" >&2; exit 2; }
# The replica runs psql but prod runs through the Management API: a meta-command
# would pass the rehearsal and then fail (harmlessly, but uselessly) on prod.
[[ -z $FIX ]] || ! grep -qE '^[[:space:]]*\\' $FIX \
  || { print -r -- "psql meta-commands (\copy, \set, …) are not supported in a ledger fix" >&2; exit 2; }
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN=${${OUT:-$REPO/.repair/run_$STAMP}:A}
CACHE=${${B3_CACHE_DIR:-$REPO/.repair/cache}:A}
export SUPABASE_WORKDIR=${SUPABASE_WORKDIR:-$REPO/.repair/supabase}
NAME=${REPLICA_NAME:-pd-rehearsal}
mkdir -p $RUN/snapshot $CACHE
rm -f $RUN/REHEARSED_OK  # a reused --out dir only counts as rehearsed once THIS run passes

PSQL=(docker exec -i $NAME psql -U postgres -v ON_ERROR_STOP=1 -q)
# Pipeline scripts against the replica. Telegram is blanked (load_dotenv never
# overrides set variables), so a rehearsal can never alert the real channel.
REPLICA=(env host=127.0.0.1 port=${REPLICA_PORT:-55432} dbname=postgres user=postgres
         password=${REPLICA_PASSWORD:-rehearsal} DB_SSLMODE=disable PORT_BOT_TOKEN= PORT_BOT_CHAT=)
MIGRATIONS=($REPO/sql/[0-9][0-9][0-9]_*.sql(N))
MIGRATIONS=(${MIGRATIONS:#*/000_*})
FIXES=()
if [[ -n $FIX ]]; then
  # Point-in-time copy of every table right before the fix: sql/001's backups
  # predate it and the backfill's own backups are taken after it.
  {
    echo "CREATE SCHEMA IF NOT EXISTS backup;"
    for t in assets trades dividends prices benchmark corporate_actions portfolio_performance; do
      echo "CREATE TABLE backup.${t}_pre_fix_${STAMP//[TZ]/} AS TABLE public.$t;"
    done
    echo "REVOKE ALL ON ALL TABLES IN SCHEMA backup FROM PUBLIC, anon, authenticated;"
  } > $RUN/ledger_backup.sql
  FIXES=($RUN/ledger_backup.sql $FIX)
fi

cleanup() { [[ ${KEEP_REPLICA:-0} == 1 ]] || docker rm -f $NAME >/dev/null 2>&1 || true; }
trap cleanup EXIT
fp_replica() { "${PSQL[@]}" --csv -c "$($PY $TOOLS/fingerprint.py query $1)" > $2; }
same() { cmp -s $1 $2 || { echo "FINGERPRINT MISMATCH: ${1:t} vs ${2:t}" >&2; diff $1 $2 >&2 || true; exit 1; } }
fail() { echo "FAILED: $1" >&2; [[ -n ${2:-} ]] && tail -30 $2 >&2; exit 1; }

echo "1/7 prod schema, snapshot and fingerprint (read-only)"
migrated=$($PY $TOOLS/sb_query.py "SELECT (to_regclass('public.corporate_actions') IS NOT NULL)::TEXT AS migrated" | tail -1)
SCHEMA=$([[ $migrated == true ]] && echo current || echo legacy)
$PY $TOOLS/snapshot.py $RUN/snapshot > $RUN/snapshot.log
$PY $TOOLS/fingerprint.py prod $SCHEMA $RUN/pre_prod.csv
echo "    prod schema: $SCHEMA"

echo "2/7 replica from the snapshot — must fingerprint-equal prod"
$TOOLS/rehearsal_up.sh $RUN/snapshot > $RUN/replica.log 2>&1 || fail "replica load" $RUN/replica.log
fp_replica $SCHEMA $RUN/pre_replica.csv
same $RUN/pre_prod.csv $RUN/pre_replica.csv

echo "3/7 migrations${FIX:+ + ledger fix} + backfill plan on the replica"
for f in $MIGRATIONS $FIXES; do
  "${PSQL[@]}" < $f > /dev/null 2> $RUN/step3.err || fail "${f:t} on the replica" $RUN/step3.err
done
(cd $REPO/pipelines && "${REPLICA[@]}" $PY -W error::FutureWarning backfill_history.py \
    --emit-sql $RUN/repair.sql --cache-dir $CACHE --report $RUN/recon.md) > $RUN/backfill.log 2>&1 \
  || fail "backfill plan" $RUN/backfill.log
"${PSQL[@]}" < $RUN/repair.sql > /dev/null || fail "repair.sql on the replica"
fp_replica current $RUN/post.csv

echo "4/7 guarded deploy.sql"
$PY $TOOLS/fingerprint.py build --pre-schema $SCHEMA $RUN/pre_prod.csv $RUN/post.csv $RUN/deploy.sql \
  $MIGRATIONS $FIXES $RUN/repair.sql > /dev/null

echo "5/7 dress rehearsal: deploy.sql on a second fresh replica"
$TOOLS/rehearsal_up.sh $RUN/snapshot > /dev/null 2>&1 || fail "second replica load"
"${PSQL[@]}" < $RUN/deploy.sql > $RUN/rehearsal_apply.log 2>&1 || fail "rehearsal apply" $RUN/rehearsal_apply.log
fp_replica current $RUN/post_rehearsal.csv
same $RUN/post.csv $RUN/post_rehearsal.csv

echo "6/7 daily TWR job on the rehearsed replica must be a no-op"
(cd $REPO/pipelines && "${REPLICA[@]}" $PY -W error::FutureWarning calculate_daily_twr.py) > $RUN/twr_after.log 2>&1 \
  || fail "TWR job" $RUN/twr_after.log
grep -q "upserted 0 row(s) (0 changed), deleted 0" $RUN/twr_after.log || fail "TWR job is not a no-op" $RUN/twr_after.log

echo "7/7 safety checks"
# a) Re-applying must fail and change nothing. With a data change the pre-state
#    guard trips; a data no-op instead stops at the per-run backup tables.
if "${PSQL[@]}" < $RUN/deploy.sql > /dev/null 2> $RUN/second_apply.err; then fail "second apply succeeded"; fi
fp_replica current $RUN/post_after_second.csv
same $RUN/post.csv $RUN/post_after_second.csv
NOOP=0
if [[ $SCHEMA == current ]] && cmp -s $RUN/pre_prod.csv $RUN/post.csv; then
  NOOP=1
  # The ERROR itself, not the idempotent migrations' "already exists, skipping" NOTICEs.
  grep -qE 'ERROR: +relation "[a-z_]+_pre_(fix|backfill)_[0-9]+" already exists' $RUN/second_apply.err \
    || fail "unexpected second-apply error" $RUN/second_apply.err
else
  grep -q "pre-state guard failed" $RUN/second_apply.err || fail "pre-state guard did not trip" $RUN/second_apply.err
fi
# b) A price row without provenance is rejected (NOT NULL fires before the FK).
if "${PSQL[@]}" -c "INSERT INTO public.prices (ticker, date, price) VALUES ('X', DATE '1999-01-04', 1)" \
    > /dev/null 2> $RUN/no_source.err; then fail "insert without provenance succeeded"; fi
grep -q 'null value in column "source"' $RUN/no_source.err || fail "unexpected provenance error" $RUN/no_source.err

{
  echo "rehearsed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "prod_schema=$SCHEMA"
  echo "data_noop=$NOOP"
  echo "ledger_fix=${FIX:-none}"
  echo "deploy_sha256=$(shasum -a 256 $RUN/deploy.sql | cut -d' ' -f1)"
} > $RUN/REHEARSED_OK
echo "OK → $RUN/deploy.sql ($(du -h $RUN/deploy.sql | cut -f1)) | data no-op: $NOOP"
echo "summary: $RUN/backfill.log (public-safe) · nominal report: $RUN/recon.md (local only)"
echo "apply:   tools/apply_deploy.sh $RUN --yes"
