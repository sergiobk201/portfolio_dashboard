#!/bin/zsh
# Fresh local Postgres replica of prod, loaded from a tools/snapshot.py snapshot.
#
# Usage:   tools/rehearsal_up.sh SNAPSHOT_DIR
# Env:     REPLICA_NAME (pd-rehearsal)  REPLICA_PORT (55432)
#          REPLICA_PASSWORD (rehearsal) REPLICA_IMAGE (postgres:17)
#
# Schema = sql/000_base_schema.sql, plus every later migration when the
# snapshot was taken after them (detected by corporate_actions.csv, which
# only exists from 001 on). Bound to 127.0.0.1 only. Stop it with
#   docker rm -f pd-rehearsal
set -euo pipefail
SNAP=${1:?usage: rehearsal_up.sh SNAPSHOT_DIR}
REPO=${0:A:h:h}
NAME=${REPLICA_NAME:-pd-rehearsal}
PORT=${REPLICA_PORT:-55432}
PASS=${REPLICA_PASSWORD:-rehearsal}
IMAGE=${REPLICA_IMAGE:-postgres:17}

docker rm -f $NAME >/dev/null 2>&1 || true
docker run -d --rm --name $NAME -e POSTGRES_PASSWORD=$PASS -p 127.0.0.1:$PORT:5432 $IMAGE >/dev/null
until docker exec $NAME pg_isready -U postgres -q; do sleep 1; done
sleep 1  # the entrypoint restarts the server once after init
until docker exec $NAME pg_isready -U postgres -q; do sleep 1; done

PSQL=(docker exec -i $NAME psql -U postgres -v ON_ERROR_STOP=1 -q)
"${PSQL[@]}" -c "CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN;"
"${PSQL[@]}" < $REPO/sql/000_base_schema.sql
if [[ -f $SNAP/corporate_actions.csv ]]; then
  for m in $REPO/sql/[0-9][0-9][0-9]_*.sql(N); do
    [[ ${m:t} == 000_* ]] && continue
    "${PSQL[@]}" < $m >/dev/null
  done
fi

# Parents before children (foreign keys → assets).
for t in assets trades dividends prices benchmark portfolio_performance corporate_actions; do
  [[ -f $SNAP/$t.csv ]] || continue
  cols=$(head -1 $SNAP/$t.csv)
  "${PSQL[@]}" -c "\\copy public.$t ($cols) FROM STDIN WITH (FORMAT csv, HEADER true)" < $SNAP/$t.csv
done

"${PSQL[@]}" >/dev/null <<'SQL'
SELECT setval(pg_get_serial_sequence('public.prices', 'price_id'), COALESCE(MAX(price_id), 1), MAX(price_id) IS NOT NULL) FROM public.prices;
SELECT setval(pg_get_serial_sequence('public.benchmark', 'bench_id'), COALESCE(MAX(bench_id), 1), MAX(bench_id) IS NOT NULL) FROM public.benchmark;
SQL
"${PSQL[@]}" -c "SELECT 'prices' AS t, COUNT(*) FROM prices UNION ALL SELECT 'trades', COUNT(*) FROM trades UNION ALL SELECT 'dividends', COUNT(*) FROM dividends UNION ALL SELECT 'benchmark', COUNT(*) FROM benchmark UNION ALL SELECT 'portfolio_performance', COUNT(*) FROM portfolio_performance"
