# Changelog

All notable changes to this project will be documented in this file.

## [0.4.0] — 2026-11-03
### Why
TWR, total capital and the other KPIs had drifted far from what the ledger implies, while the three scheduled jobs reported success (started 5–6 h late, wrote wrong numbers).

### Root causes
1. Bulk-loaded history used **dividend-adjusted** closes (median 4.6 % off; ~21k rows off by > 2 %) → income double-counted / mis-valued.
2. **Splits and bonus issues were never applied** to share counts (19 events, e.g. 1:5, 1:10, 18 % bonus).
3. New tickers and **ticker renames** (ELET3→AXIA3, EMBR3→EMBJ3, MRFG3→MBRF3) left valuation gaps.
4. TWR job recomputed a **35-day window from a frozen anchor**, so any late correction never propagated.
5. No unique keys: duplicate and **holiday rows** in `prices`; SPY rows **mislabelled one session late** in `benchmark`.
6. Provider artefacts (zero-volume carried quotes) stored as closes.
7. Gmail parser: CBLC **fee sign flipped on sell notes** (negative fees).
8. Public Actions logs printed nominal values; alert spam on every run.

### Added
- `portfolio_engine.py`: pure, vectorized engine — positions with corporate actions, CA-aware price carry, closed-form implied contributions, start-of-day-flow TWR, validation issues (ERROR blocks writes), average-cost ledger cross-check, income/position reconciliation, XIRR.
- `market_data.py`: B3 COTAHIST parser (official closes), exchange calendars (BVMF/XNYS), Yahoo raw (split un-adjusted) and BRAPI fallbacks.
- `backfill_history.py`: one-transaction historical repair with backups, convergence check, investor stats and a local-only nominal reconciliation + IRPF monthly view.
- `sql/001_integrity_corporate_actions.sql`: unique keys, mandatory price/benchmark provenance, `corporate_actions` table, `is_trading_day`, backups in a non-API schema.
- `nota_fees.py`: signed CBLC fee formula (debit-positive), unit-tested.
- `tests/`: 62 synthetic-data tests (accounting identities, unitised-NAV equivalence, split/bonus/ex-date entitlement, COTAHIST layout, calendars, diff-sync, fee math); mutation-checked.
- `.github/workflows/daily_pipeline.yml` (test gate → prices + SPY → performance, idempotent retry, keepalive) and `tests.yml`.

### Changed
- Prices job: official B3 close first, explicit no-trade markers, automatic upgrade of fallback rows, provider-disagreement alerts.
- TWR job: full recompute every run, diff-only writes (`updated_at` = revision audit trail), shrink guard, revision alerts.
- Pinned runtime dependencies; logs carry dates, counts and percentages only (amounts go to the private Telegram channel).

### Removed
- `daily_prices.yml`, `daily_spy.yml`, `daily_twr.yml` (superseded).

### Lessons
- Never store adjusted prices: adjust in the engine, from events, where it is auditable.
- A green CI run is not a correct number — validate outputs (identities, reconciliations), not just exit codes.
- Annual COTAHIST files are not date-ordered within a ticker; always sort before any time-series logic.
- Deploy data repairs as one guarded transaction: assert the exact pre-state and the rehearsed post-state, or roll back.

## [Unreleased]
### Added
- `public_app.py`: Full Streamlit implementation with interactive dashboards.
- **Advanced Quant Metrics:**
  - Annualized Volatility calculation for portfolio returns.
  - **Sharpe Ratio:** Textbook implementation using daily excess returns over risk-free rate.
  - **Sortino Ratio:** Downside deviation calculation using risk-free rate as the Minimal Acceptable Return (MAR).
- **Risk-Free Rate Engine:** Integration with `yfinance` to fetch 13-week US T-bill yields (^IRX) with auto-alignment and interpolation.
- **Supabase Integration:** Vectorized data fetching for `portfolio_performance`, `prices`, `trades`, and `dividends`.

### Changed
- **KPI Layout:** Consolidated "vs SPY" comparison into the primary YTD Return tile.
- **Stats Card:** Replaced redundant YTD comparison with a dedicated "Portfolio Stats" card for Vol/Sharpe/Sortino.
- Refined TWR logic to handle both decimal factor and percentage formats from database sources.
- Implemented robust error handling for external benchmark (SPY) data fetching via `yfinance`.
- Optimized database connections using `@st.cache_resource` for singleton pooling.

### Reflections & Challenges
- **SQL Data Handling:** Resolved a `NoneType` error caused by malformed SQL syntax and missing single quotes in date filtering.
- **Vectorization:** Ensured `numpy` operations on `daily_return` handles potential `float` vs `Decimal` type mismatches from the database.
- **Layout Fidelity:** Used custom HTML/CSS within Streamlit to achieve the dual-delta layout from the `mockup.png`.
- **Investor Transparency:** Pivoting toward Phase 4 to support "Public Mode" for external reporting, masking nominal currency figures.
