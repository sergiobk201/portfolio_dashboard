# Changelog

All notable changes to this project will be documented in this file.

## [0.5.0] — 2026-11-03
### Why
After 0.4.0 the stored numbers were right, but the public dashboard still computed its own holdings and YTD from raw trades and prices. It ignored the 19 corporate actions (5:1 splits showed as −80 %), used a USD T-bill rate and a USD price-only SPY, and swallowed every error in `except Exception: pass`.

### Added
- `pipelines/dashboard_metrics.py`: pure, tested metrics behind the page.
  - Holdings come from the engine's `build_positions`, and closes are restated in today's share basis.
  - YTD runs from the last close before Jan 1, and risk statistics use B3 sessions.
  - CDI accrues by the BCB convention, and SPY is a total-return index in BRL at PTAX.
  - Per-event DY is exact (`total ÷ quantity`) and excludes FRAÇÕES.
- `tests/test_public_app.py`: AppTest smoke tests (fake DB, BCB and Yahoo). They cover the full render, degraded feeds, the segment filter and a DB outage, and assert the read-only session and weights-only chart payloads.
- `.streamlit/config.toml` pins the dark theme (light-mode browsers rendered white-on-white text).
- `requirements-monthly.txt`: pinned stack for the Gmail and dividends imports. `pdfplumber`, `pikepdf` and `openpyxl` were missing from every requirements file.
- `tools/` guarded-repair runbook, `.env.example`, Dependabot (monthly, grouped) and a `compileall` CI gate.
- `tools/dashboard_role.sql`: optional least-privilege `dashboard_ro` login (SELECT only, return columns only on `portfolio_performance`, read-only by default), verified on a prod replica.
- `pipelines/ledger_dedupe.py`: pure idempotency check on what the broker printed. Fee, portfolio and ID are not compared, and identical fills are counted, not collapsed.

### Changed
- `public_app.py` rebuilt on `dashboard_metrics`:
  - It runs read-only (`BEGIN READ ONLY` per transaction, safe behind a pooler) and reconnects once when the pooler drops the connection.
  - It catches specific exceptions only. A DB outage shows a message without connection details.
  - Feed failures fall back visibly (SPY price/USD, rf = 0).
- Privacy: the app no longer queries `portfolio_value`. The sector donut receives weights, not BRL amounts, which the old `px.pie` serialised to the browser.
- `requirements.txt` is now the pinned dashboard stack for Python 3.10–3.14. Unpinned, Python 3.12 resolved pandas 3.1 and numpy 2.5, majors nobody had tested. Streamlit Cloud runs 3.14, where numpy 2.2 has no wheels, so numpy is pinned per Python version. A `dashboard-cloud` CI job installs the file alone on 3.14, as Cloud does.
- `tests.yml` installs the dashboard stack too. The daily gate stays pipeline-only, and the AppTest suite skips there.
- Monthly loaders (`gmail_automation.py`, `dividends_pipeline.py`) are now safe to re-run:
  - Rows already stored are skipped and counted.
  - A failed read stops the run before any write, and a failed insert raises instead of printing success.
  - Specific exceptions replace `except Exception`.
  - A statement with no blank row after the data no longer loads zero rows.
- `gmail_automation.py` imports every nota e-mailed in the last 45 days (`--days N`), oldest first, instead of only the newest:
  - Each nota gets a fresh importer and a fresh ledger read, so a nota re-sent in the same window loads once.
  - E-mails without a nota PDF or without Bovespa lines are reported and skipped.
  - The batch stops at the first failure, names the nota and exits 1; the dividends import also exits 1 on failure.
  - PDFs are deleted after each nota unless `--keep-pdfs`.
- Nota trade lines must reconcile (quantity × price = total). Quantities and prices of 1.000 or more now parse, and a misread line (a new layout, a termo or options line) stops the nota.

### Removed
- `testing_scripts/*.ipynb`: obsolete prototypes with real ledger output and a hardcoded PDF password. They and the other data files (a decrypted statement PDF, a dividend CSV, two notebooks) were purged from git history with `git filter-repo` on 2026-11-03.

### Lessons
- Test the failure path: the AppTest suite found that every feed outage crashed the page (an empty fallback `Series` had a RangeIndex).
- A 2-decimal per-share column is not a per-share amount. Derive it from `total ÷ quantity`.
- A chart's payload is public even when the axis labels are not: send weights, never amounts.
- Let the numbers check themselves. A parser anchored on "the first all-digit token" loads a wrong trade the day the layout shifts; quantity × price = total turns that into a stop.

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
