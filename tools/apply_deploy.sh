#!/bin/zsh
# Apply a rehearsed deploy.sql to PROD in one transaction, then prove that
# prod is fingerprint-identical to the rehearsal.
#
# Usage: tools/apply_deploy.sh RUN_DIR --yes
# Env:   PYTHON (python3)  SUPABASE_WORKDIR (.repair/supabase)
#
# deploy.sql carries its own guards: it rolls back unless prod still equals the
# snapshot the plan was built from AND ends equal to the rehearsal.
set -euo pipefail
TOOLS=${0:A:h}
REPO=${TOOLS:h}
RUN=${${1:?usage: apply_deploy.sh RUN_DIR --yes}:A}
[[ ${2:-} == --yes ]] || { echo "refusing to write to PROD without --yes" >&2; exit 2; }
PY=${PYTHON:-python3}
export SUPABASE_WORKDIR=${SUPABASE_WORKDIR:-$REPO/.repair/supabase}

MANIFEST=$RUN/REHEARSED_OK
for f in $MANIFEST $RUN/deploy.sql $RUN/pre_prod.csv $RUN/post.csv; do
  [[ -f $f ]] || { echo "$RUN is not a rehearsed run (no ${f:t}) — use tools/rebuild.sh first" >&2; exit 2; }
done
want=$(sed -n 's/^deploy_sha256=//p' $MANIFEST)
have=$(shasum -a 256 $RUN/deploy.sql | cut -d' ' -f1)
[[ $want == $have ]] || { echo "deploy.sql changed after the rehearsal — rebuild" >&2; exit 2; }

# What prod holds after a failed call: the transaction is atomic, so it is
# either the snapshot, the rehearsal, or a state someone else wrote since.
prod_state() {
  local schema
  schema=$(sed -n 's/^prod_schema=//p' $MANIFEST)
  $PY $TOOLS/fingerprint.py prod $schema $RUN/prod_now.csv
  if cmp -s $RUN/pre_prod.csv $RUN/prod_now.csv; then
    echo "prod is unchanged (rolled back)"
  elif $PY $TOOLS/fingerprint.py prod current $RUN/prod_now.csv 2>/dev/null && cmp -s $RUN/post.csv $RUN/prod_now.csv; then
    echo "yet prod equals the rehearsal: the commit landed and only the reply was lost"
  else
    echo "prod changed after the snapshot (e.g. a daily run) and nothing was applied — run tools/rebuild.sh again"
  fi
}

echo "applying $(du -h $RUN/deploy.sql | cut -f1) to PROD in one transaction…"
if ! supabase db query --linked --workdir $SUPABASE_WORKDIR -f $RUN/deploy.sql > $RUN/prod_apply.out 2>&1; then
  { grep -m3 -E "guard failed|ERROR|error" $RUN/prod_apply.out || tail -3 $RUN/prod_apply.out; } >&2
  if grep -q "pre-state guard failed" $RUN/prod_apply.out; then
    echo "ROLLED BACK by the pre-state guard — prod unchanged. It moved since the snapshot: run tools/rebuild.sh again" >&2
  elif grep -q "post-state guard failed" $RUN/prod_apply.out; then
    echo "ROLLED BACK by the post-state guard — prod unchanged. The SQL did not reproduce the rehearsal on prod: investigate before rebuilding" >&2
  else
    echo "deploy call FAILED — checking prod (read-only)…" >&2
    echo "→ $(prod_state)" >&2
  fi
  exit 1
fi
$PY $TOOLS/fingerprint.py prod current $RUN/post_prod.csv
if ! cmp -s $RUN/post.csv $RUN/post_prod.csv; then
  echo "prod differs from the rehearsal:" >&2
  diff $RUN/post.csv $RUN/post_prod.csv >&2 || true
  exit 1
fi
echo "OK — prod is fingerprint-identical to the rehearsal ($RUN/post_prod.csv)"
