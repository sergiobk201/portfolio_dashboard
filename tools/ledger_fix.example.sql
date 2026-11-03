-- Template for tools/rebuild.sh --ledger-fix FILE.sql
--
-- Keep real fixes OUT of the repository (they carry ledger data): put them in
-- .repair/ (git-ignored). Plain SQL only — the deploy goes through the
-- Management API, so psql meta-commands (\copy, \set, …) are not available.
--
-- Each correction must assert it touched exactly the row(s) you meant, so a
-- stale or mistyped fix aborts the whole deploy. The fix runs after the
-- migrations; rebuild.sh snapshots every table to backup.<table>_pre_fix_<stamp>
-- right before it, inside the same transaction.

DO $fix$
DECLARE
    n INT;
BEGIN
    UPDATE public.trades
    SET quantity = 0              -- corrected value
    WHERE trade_id = '<trade_id>'
      AND ticker = '<TICKER>'
      AND quantity = 0;           -- value being replaced (makes the fix one-shot)
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> 1 THEN
        RAISE EXCEPTION 'ledger fix matched % rows (expected exactly 1)', n;
    END IF;
END
$fix$;
