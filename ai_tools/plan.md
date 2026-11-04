# Portfolio Dashboard: Implementation Plan

## 🎯 Objective
Build a professional, investor-grade dashboard for transparent quarterly reporting, ingesting dividends, trades, and prices from Supabase into a Python-based frontend.

## 📅 Timeline: April 2026
- **Phase 1: Research & Setup (Days 1-2):** Map database schema and select frontend components.
- **Phase 2: Streamlit Prototype (Days 3-7):** Functional v1 dashboard.
- **Phase 3: Refinement & Dash Migration (Days 8-14):** Advanced styling and interactive reporting features.
- **Phase 4: Investor-Ready Dashboard (Public Mode):** Masking BRL amounts and focusing on relative performance.

## 🗺 Roadmap

### [X] Phase 1: Preparation
- [X] Verify Supabase schema for `dividends`, `trades`, and `daily_prices`.
- [X] Confirm TWR (Time-Weighted Return) calculation consistency.
- [X] Research Streamlit-friendly financial visualization libraries (Selected: Plotly).

### [X] Phase 2: Streamlit v1 Implementation
- [X] Implement data fetching logic for Supabase (using `psycopg2`).
- [X] Build "Portfolio Overview" (Total Value, TWR, Asset Allocation).
- [X] Build "Dividend Tracker" (Annual Yield, Monthly Income, Forecast).
- [X] Build "Trade History" (Recent Activity, P&L by position).
- [X] Add sidebar filters for Date Range (Quarterly/Yearly) and Ticker.

### [X] Phase 3: Reporting & Polish
- [X] Implement interactive Plotly charts for cumulative returns vs. benchmark (S&P 500).
- [X] Style the app with custom CSS to match an institutional/professional look.
- [X] Add advanced risk metrics: Annualized Volatility, Sharpe Ratio, and Sortino Ratio.
- [X] Integrate 13-week T-bill yield (^IRX) as the risk-free rate proxy (replaced by CDI in v0.5.0).
- [X] Consolidate KPI tiles for cleaner benchmark comparison.

### [X] Phase 4: Investor-Ready Dashboard (Public Mode)
- [X] ~~"Public Mode" toggle~~ superseded: the app is public-only. It opens a read-only session, never queries nominal values and sends only percentages and weights to the browser.
- [X] "Top Holdings" and "Trades" show % weights. Units are deliberately hidden, because units × public prices would reveal BRL amounts.
- [X] Metrics standardised on TWR, CDI-based Sharpe/Sortino, SPY total return in BRL and exact per-event DY %.
- [X] No nominal figure is inferable: the chart payloads carry only rebased % and weights (asserted in `tests/test_public_app.py`).
- [ ] Relative contribution per holding and segment (share of TWR). Not built yet.

### [X] Phase 5: Nov 2026 rebuild
Trigger: the scheduled jobs reported success while TWR, total capital and the other KPIs had drifted away from what the ledger implies.
- [X] Root-caused the drift:
  - dividend-adjusted closes stored as raw prices;
  - splits and bonuses never applied to share counts;
  - ticker renames left valuation gaps;
  - TWR recomputed from a frozen window;
  - duplicate and holiday price rows;
  - SPY rows dated one session late;
  - the CBLC fee sign was flipped on sell notes.
- [X] `portfolio_engine.py`: a pure, vectorized engine. It covers corporate-action-aware positions, implied contributions, start-of-day-flow TWR, validation gates and XIRR.
- [X] Official B3 COTAHIST closes with per-row provenance. Fallback rows are upgraded automatically.
- [X] `sql/001`: unique keys, mandatory provenance, `corporate_actions`, `is_trading_day`, and backups in a non-API schema.
- [X] Historical backfill deployed to prod as one rehearsed, fingerprint-guarded transaction.
- [X] One gated daily workflow (tests → prices + SPY → full recompute), with an idempotent retry and a schedule keepalive. The old workflows are retired.
- [X] CI: synthetic-data test suite, `compileall` syntax gate, and monthly grouped Dependabot updates.
- [X] `tools/`: reusable guarded-repair flow (rebuild → dress rehearsal → apply) with a runbook. Ledger fixes are backed up automatically.
- [X] Dashboard statistics moved onto the engine (`dashboard_metrics.py`):
  - corporate-action-aware holdings and YTD;
  - risk statistics on B3 sessions;
  - CDI as the risk-free rate;
  - SPY total return in BRL as the benchmark.
- [X] README: monthly routine, Telegram alert guide, repairs runbook and methodology. Obsolete prototype notebooks removed.

## 🚀 Next Steps
- [x] Gmail import: process every nota in the Gmail search window, not only the newest, so the monthly cadence cannot skip a trading day. Done in 0.5.0: a 45-day window (`--days N`), idempotent, with line-by-line reconciliation.
- [ ] Move the pipelines and CI off Python 3.10 (end of life since October 2026) to 3.13. numpy 2.2.6 and pandas 2.3.3 already support 3.13, so the numbers stay identical; then lift Dependabot's numpy/pandas freeze.
- [ ] Model withdrawals before the first one happens. The engine currently assumes contributions only.
- [ ] IRPF: add the *custo atribuído* of bonus shares to the cost basis.
