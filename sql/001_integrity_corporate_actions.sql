-- 001_integrity_corporate_actions.sql
-- Integrity keys, provenance and corporate actions for the full-recompute
-- pipeline (pipelines/portfolio_engine.py).
--
-- Idempotent and transactional: re-running it is a no-op. Apply it BEFORE the
-- new pipelines run — they rely on these unique keys for ON CONFLICT.

BEGIN;

-- 0. Point-in-time copies of every table this migration or the backfill touches.
--    The backup schema is not exposed through the API roles.
CREATE SCHEMA IF NOT EXISTS backup;
REVOKE ALL ON SCHEMA backup FROM PUBLIC;
REVOKE ALL ON SCHEMA backup FROM anon, authenticated;

CREATE TABLE IF NOT EXISTS backup.prices_pre_001 AS TABLE public.prices;
CREATE TABLE IF NOT EXISTS backup.benchmark_pre_001 AS TABLE public.benchmark;
CREATE TABLE IF NOT EXISTS backup.portfolio_performance_pre_001 AS TABLE public.portfolio_performance;
CREATE TABLE IF NOT EXISTS backup.trades_pre_001 AS TABLE public.trades;
CREATE TABLE IF NOT EXISTS backup.dividends_pre_001 AS TABLE public.dividends;
REVOKE ALL ON ALL TABLES IN SCHEMA backup FROM PUBLIC, anon, authenticated;

-- 1. prices: one close per (ticker, session), with provenance.
--    Every duplicate group carries identical prices (verified 2026-11-03),
--    so which row survives does not change any value.
WITH ranked AS (
    SELECT price_id,
           ROW_NUMBER() OVER (PARTITION BY ticker, date ORDER BY created_at DESC, price_id DESC) AS rn
    FROM public.prices
)
DELETE FROM public.prices AS p
USING ranked AS r
WHERE p.price_id = r.price_id
  AND r.rn > 1;

-- source is mandatory with NO default: a writer that does not declare
-- provenance (e.g. a pre-2026-11 job still scheduled somewhere) fails loudly
-- instead of inserting rows nobody can audit later.
ALTER TABLE public.prices ADD COLUMN IF NOT EXISTS source TEXT;
UPDATE public.prices SET source = 'legacy' WHERE source IS NULL;
ALTER TABLE public.prices
    ALTER COLUMN source DROP DEFAULT,
    ALTER COLUMN source SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT CURRENT_DATE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'prices_ticker_date_key') THEN
        ALTER TABLE public.prices ADD CONSTRAINT prices_ticker_date_key UNIQUE (ticker, date);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'prices_source_check') THEN
        ALTER TABLE public.prices ADD CONSTRAINT prices_source_check
            CHECK (source IN ('b3_cotahist', 'b3_no_trade', 'yahoo_raw', 'brapi', 'legacy', 'manual'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'prices_price_positive') THEN
        ALTER TABLE public.prices ADD CONSTRAINT prices_price_positive CHECK (price > 0);
    END IF;
END $$;

COMMENT ON COLUMN public.prices.price IS
    'RAW session close as printed (never dividend- or split-adjusted). For b3_no_trade rows: last print, corporate-action adjusted (informational).';
COMMENT ON COLUMN public.prices.source IS
    'b3_cotahist = official B3 close; b3_no_trade = B3 confirmed no trade that session; yahoo_raw / brapi = fallback providers; legacy = pre-2026-11 rows; manual = hand-entered.';

-- 2. benchmark: one SPY close per NYSE session, with provenance.
WITH ranked AS (
    SELECT bench_id,
           ROW_NUMBER() OVER (PARTITION BY date ORDER BY created_at DESC, bench_id DESC) AS rn
    FROM public.benchmark
)
DELETE FROM public.benchmark AS b
USING ranked AS r
WHERE b.bench_id = r.bench_id
  AND r.rn > 1;

ALTER TABLE public.benchmark ADD COLUMN IF NOT EXISTS source TEXT;
UPDATE public.benchmark SET source = 'legacy' WHERE source IS NULL;
ALTER TABLE public.benchmark
    ALTER COLUMN source DROP DEFAULT,
    ALTER COLUMN source SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT CURRENT_DATE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'benchmark_date_key') THEN
        ALTER TABLE public.benchmark ADD CONSTRAINT benchmark_date_key UNIQUE (date);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'benchmark_source_check') THEN
        ALTER TABLE public.benchmark ADD CONSTRAINT benchmark_source_check
            CHECK (source IN ('fmp', 'yahoo_raw', 'legacy', 'manual'));
    END IF;
END $$;

-- 3. trades: reject rows the engine cannot interpret (all existing rows pass).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'trades_side_check') THEN
        ALTER TABLE public.trades ADD CONSTRAINT trades_side_check CHECK (side IN ('BUY', 'SELL'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'trades_quantity_positive') THEN
        ALTER TABLE public.trades ADD CONSTRAINT trades_quantity_positive CHECK (quantity > 0);
    END IF;
END $$;

-- 4. corporate_actions: share-count events applied on the ex-date as
--    new_shares = floor(old_shares * ratio); B3 auctions the fractions.
CREATE TABLE IF NOT EXISTS public.corporate_actions (
    ticker      VARCHAR NOT NULL,
    ex_date     DATE NOT NULL,
    ratio       NUMERIC(18, 10) NOT NULL,
    action_type TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT corporate_actions_pkey PRIMARY KEY (ticker, ex_date),
    CONSTRAINT corporate_actions_ticker_fkey FOREIGN KEY (ticker)
        REFERENCES public.assets (ticker) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT corporate_actions_ratio_positive CHECK (ratio > 0),
    CONSTRAINT corporate_actions_type_check CHECK (action_type IN ('SPLIT', 'REVERSE_SPLIT', 'BONUS')),
    CONSTRAINT corporate_actions_source_check CHECK (source IN ('yahoo', 'manual'))
);
ALTER TABLE public.corporate_actions ENABLE ROW LEVEL SECURITY;
COMMENT ON TABLE public.corporate_actions IS
    'Splits, reverse splits and bonus issues. ratio = new shares per old share (5 = 1:5 split, 0.1 = 10:1 reverse, 1.1 = 10% bonus).';

-- 5. portfolio_performance: flag B3 sessions so statistics skip calendar fill days.
ALTER TABLE public.portfolio_performance
    ADD COLUMN IF NOT EXISTS is_trading_day BOOLEAN NOT NULL DEFAULT FALSE;
COMMENT ON TABLE public.portfolio_performance IS
    'One row per calendar day, rebuilt from the ledger by pipelines/calculate_daily_twr.py. Use is_trading_day for return statistics.';

-- 6. The SQL equity functions ignore corporate actions and the stale-price
--    policy; keep them for old clients but mark them deprecated.
DO $$
BEGIN
    IF to_regprocedure('public.get_equity_value(date)') IS NOT NULL THEN
        COMMENT ON FUNCTION public.get_equity_value(date) IS
            'DEPRECATED 2026-11: ignores corporate actions. Read portfolio_performance instead.';
    END IF;
    IF to_regprocedure('public.get_equity_values_range(date, date)') IS NOT NULL THEN
        COMMENT ON FUNCTION public.get_equity_values_range(date, date) IS
            'DEPRECATED 2026-11: ignores corporate actions. Read portfolio_performance instead.';
    END IF;
END $$;

COMMIT;
