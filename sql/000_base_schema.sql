-- 000_base_schema.sql
-- Reference DDL of the production schema as it stood on 2026-11-03, BEFORE
-- 001. Not a migration for production (the tables exist there); it builds an
-- identical local database for tests and rehearsals.

CREATE TABLE IF NOT EXISTS public.assets (
    ticker       VARCHAR PRIMARY KEY,
    company_name VARCHAR,
    sector       VARCHAR,
    industry     VARCHAR
);

CREATE TABLE IF NOT EXISTS public.trades (
    trade_id  TEXT PRIMARY KEY,
    date      DATE NOT NULL,
    side      VARCHAR NOT NULL,
    ticker    VARCHAR NOT NULL,
    asset     TEXT NOT NULL,
    quantity  INTEGER NOT NULL,
    price     NUMERIC(10, 4) NOT NULL,
    total     NUMERIC(12, 2) NOT NULL,
    fee       NUMERIC(8, 2) DEFAULT 0.00,
    portfolio TEXT NOT NULL,
    CONSTRAINT fk_ticker FOREIGN KEY (ticker)
        REFERENCES public.assets (ticker) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.dividends (
    dividend_id   TEXT PRIMARY KEY,
    dividend_date DATE NOT NULL,
    ticker        VARCHAR NOT NULL,
    type          VARCHAR NOT NULL,
    dividend      NUMERIC(10, 2) NOT NULL,
    total         NUMERIC(10, 2) NOT NULL,
    quantity      INTEGER NOT NULL,
    portfolio     TEXT NOT NULL,
    CONSTRAINT fk_ticker FOREIGN KEY (ticker)
        REFERENCES public.assets (ticker) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.prices (
    price_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date       DATE NOT NULL,
    ticker     VARCHAR NOT NULL,
    price      NUMERIC(12, 2) NOT NULL,
    created_at DATE NOT NULL,
    CONSTRAINT fk_assets_ticker FOREIGN KEY (ticker)
        REFERENCES public.assets (ticker) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.benchmark (
    bench_id   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date       DATE NOT NULL,
    price      NUMERIC(10, 2) NOT NULL,
    created_at DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS public.portfolio_performance (
    date            DATE PRIMARY KEY,
    portfolio_value NUMERIC(18, 4) NOT NULL,
    equity_value    NUMERIC(18, 4) NOT NULL,
    uninvested_cash NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_invested  NUMERIC(18, 4) NOT NULL,
    external_inflow NUMERIC(18, 4) NOT NULL DEFAULT 0,
    daily_return    NUMERIC(18, 8) NOT NULL DEFAULT 0,
    twr             NUMERIC(18, 8) NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT price_performance_date_key UNIQUE (date)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_performance_date ON public.portfolio_performance (date);

ALTER TABLE public.assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.dividends ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.benchmark ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.portfolio_performance ENABLE ROW LEVEL SECURITY;
