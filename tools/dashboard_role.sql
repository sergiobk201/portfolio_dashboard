-- Least-privilege login for the PUBLIC dashboard (public_app.py).
--
-- The app needs SELECT on seven tables and nothing else. portfolio_performance
-- is limited to its return columns, so even a leaked dashboard password can
-- neither write nor read a nominal portfolio value. The ledger pipelines keep
-- using the postgres login; only the Streamlit secrets change.
--
-- Usage (once; re-running is safe and rotates the password):
--   1. cp tools/dashboard_role.sql .repair/dashboard_role.sql   (.repair/ is git-ignored)
--   2. Replace <STRONG-PASSWORD> in the COPY (24+ chars, e.g. `openssl rand -base64 32`).
--   3. supabase db query --linked -f .repair/dashboard_role.sql   (or the SQL editor), then delete the copy.
--   4. Streamlit Cloud → App settings → Secrets:
--        user = "dashboard_ro.<project-ref>"   # Session pooler; plain "dashboard_ro" on a direct host
--        password = "<STRONG-PASSWORD>"
--
-- Plain SQL only (no psql meta-commands): it runs through the Management API.

BEGIN;

DO $$
DECLARE
    pw TEXT := '<STRONG-PASSWORD>';
BEGIN
    IF pw LIKE '<%' OR length(pw) < 24 THEN
        RAISE EXCEPTION 'dashboard_role.sql: set a password of at least 24 characters first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        EXECUTE format('CREATE ROLE dashboard_ro LOGIN NOINHERIT PASSWORD %L', pw);
    ELSE
        EXECUTE format('ALTER ROLE dashboard_ro WITH LOGIN NOINHERIT PASSWORD %L', pw);
    END IF;
END $$;

-- Defense in depth: the app already opens BEGIN READ ONLY transactions.
ALTER ROLE dashboard_ro SET default_transaction_read_only = on;
ALTER ROLE dashboard_ro SET statement_timeout = '15s';

-- Reset, then grant exactly what public_app.py queries (column grants are
-- revoked together with the table grant, so re-runs never accumulate).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM dashboard_ro;
GRANT USAGE ON SCHEMA public TO dashboard_ro;
GRANT SELECT ON public.prices, public.trades, public.dividends, public.corporate_actions,
                public.assets, public.benchmark
    TO dashboard_ro;
GRANT SELECT (date, daily_return, twr, is_trading_day) ON public.portfolio_performance TO dashboard_ro;

-- RLS is enabled with no policies (deny-all for API roles). Open SELECT to this
-- role only; anon / authenticated stay denied.
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['prices', 'trades', 'dividends', 'corporate_actions', 'assets', 'benchmark',
                             'portfolio_performance'] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = t AND policyname = 'dashboard_ro_read'
        ) THEN
            EXECUTE format('CREATE POLICY dashboard_ro_read ON public.%I FOR SELECT TO dashboard_ro USING (true)', t);
        END IF;
    END LOOP;
END $$;

COMMIT;
