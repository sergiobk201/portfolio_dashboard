# Changelog

All notable changes to this project will be documented in this file.

## [0.6.0] — 2026-11-10
### Why
A four-part review of the Nov 3 rebuild (c794ee3..ecd6c08) found defects the test suite could not see:
- a privacy leak on the public page;
- two ways to apply a corporate action wrongly;
- warnings that never reached Telegram;
- a trade import that checked each line but never the nota as a whole;
- a segment YTD weighted toward winners.

### Fixed
- **Privacy** (`public_app.py`): the sector donut sent full-precision weights to the browser, and the donut follows the ticker filter. With two tickers in different sectors, the exact weights plus public closes give away the share counts. Weights now leave the server rounded to 0.1 %, like every table.
- **Corporate actions applied twice** (`prices_pipeline.sync_corporate_actions`, `backfill_history.plan_corporate_actions`):
  - The sync matched a Yahoo event to stored rows on the exact ex-date only, so an event Yahoo dated on a different day than the stored row was inserted again. That covers a hand-corrected date, and Yahoo dating on the data-com, the ex-date or the credit day.
  - Any row for the ticker within 10 days (`CA_MATCH_DAYS`) now counts as the same event, whatever its ratio. Within one run, Yahoo events that close collapse into one.
  - In the backfill, the hand-verified manual row wins.
- **Repeating ratios** (`portfolio_engine.entitled_shares`): `ratio` is NUMERIC(18,10), so 1/3 is stored as 0.3333333333 and 300 shares became 99. A product within that storage rounding of a whole number now counts as whole, in both `build_positions` and `average_cost_ledger`.
- **Silent warnings** (`calculate_daily_twr.build_alerts`):
  - Only warnings dated after the stored end raised an alert, so anything a back-dated import revealed never reached Telegram; 14 warnings were open.
  - Every warning on a day the run wrote now raises an alert, and a retry stays quiet.
  - The two income warnings now carry a date.
- **Trade import** (`gmail_automation.py`):
  - Each nota now reconciles to the cent with its own printed *Compras / Vendas à vista* before any database read, so a missed line fails the nota instead of loading part of it.
  - A Bovespa nota with no recognised line fails instead of passing as NO_TRADES.
  - A PDF that spans two trading days fails.
  - Quantity, price and value are read from the right, so an observation code is never taken for the quantity.
  - A cost cap (1 % of volume + R$ 10) catches a misread D/C flag.
- **Segment YTD** (`dashboard_metrics.weighted_ytd`): returns were weighted by end value, so equal stakes at +100 % and −50 % read +70 %. YTD is now the basket's return, Σ value ÷ Σ Jan 1 value − 1, which gives +25 %.
- **Dashboard (lower severity)**:
  - Per-ticker YTD starts at the last B3 session before Jan 1. A re-bought ticker had started from a close in its earlier holding period.
  - Dividend yields key a split on the record date, inferred from the row's quantity.
  - CDI growth is credited on the next rate date, so a holiday's level no longer includes the next overnight.
- **Daily prices (lower severity)**:
  - Network time is bounded: 120 s per B3 file; B3 counts as down after 3 consecutive failures or 600 s; the whole fetch phase gets 1,200 s. Whatever was fetched is still written.
  - B3 files download newest-first, as the docstring always said.
  - A fallback close that B3 later shows as untraded becomes a no-trade marker.
- **Alerts** (`alerts.fit`): a long message is cut on a line boundary with every tag closed. A message Telegram rejects (400) is sent once more as plain text.
- **Imports (lower severity)**:
  - Ticker answers are validated, and Ctrl-D stops the import instead of saving a placeholder. This applies to both imports.
  - Assets and trades are written in one transaction with CSV COPY, which fixes an industry name with a comma splitting into an extra column.
  - In the income import, a blank row mid-sheet no longer drops the lines after it, and "1.500" now parses as 1,500 shares.
  - Income with no date fails.
  - Credits dated after today are skipped as provisioned.
  - Amounts are rounded half-up to the cent before the duplicate check.
- A ratio-1 (rejected) corporate action no longer hides the fractions warning.

### Added
- Telegram: "🕒 B3 file for *date* unavailable", "⚠️ Corporate-action check skipped", and a count of open warnings by code in the digest.
- `--all-warnings`, or `gh workflow run daily_pipeline.yml -f all_warnings=true`, lists every open warning.
- `pipelines/prompts.py`: one prompt loop for both monthly imports.
- Tests for every fix, each mutation-checked: it fails when its fix is reverted.

### Not changed
- CDI and SPY YTD keep their own year-end base, the calendar-period convention. B3 does not trade on Dec 31, so its Dec 31 value is the Dec 30 close.
- Duplicate check across notas: a deterministic `trade_id` ("<Nr. nota>-<line>") would let the database reject a re-inserted line without a migration. It is not needed while Rico issues one nota per trading day.

### Lessons
- An alert keyed to "after the last run" misses everything a back-dated import reveals. Key it to what the run wrote.
- A per-line check cannot see a missing line. Reconcile against the document's own totals.
- The database rounds too. NUMERIC(18,10) ratios and NUMERIC(10,2) amounts must be modelled wherever they are compared.
- Anything the browser receives is public, even when it is never displayed.

## [0.5.1] — 2026-11-10
### Why
The daily Telegram messages stopped after 0.4.0. Before it, the TWR job sent "🔴 Cumulative TWR is negative" on almost every run from Aug 4 to Nov 1, because the pre-0.4.0 series sat between −6 % and +3 %. The corrected series is about +30 %. 0.4.0 rightly removed that alarm as spam, but it was also the only message on a healthy day. A working pipeline and a broken channel then looked the same: silence. A run that died before a script could alert (the test gate, setup, a crash, a DB outage) never reached Telegram either.

### Added
- Daily digest (`calculate_daily_twr.py`): one Telegram message per B3 session the run appends.
  - It carries the session's return, the BRL P&L net of contributions, MTD, YTD, TWR since inception and value, all as of the session close.
  - A catch-up run reports every appended session at once, so each change in value lands in exactly one digest.
  - Retries and non-session days send nothing.
- `notify-failure` job (`daily_pipeline.yml`): when any job fails or times out, Telegram gets each job's result and the run link.
- Tests for the digest: the span, the MTD/YTD bases, P&L net of inflows, the catch-up and first-run cases. All mutation-checked.

### Lessons
- Silence is not a signal. An alert-only channel needs a heartbeat, or a broken pipeline looks like a healthy one.
- An alert that fires every day is a symptom. "TWR is negative" was the old engine's error, reported as news.

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
- `requirements.txt` is now the pinned dashboard stack for Python 3.11–3.14 (Streamlit Cloud runs 3.14, where numpy 2.2 has no wheels). Unpinned, Python 3.12 resolved pandas 3.1 and numpy 2.5, majors nobody had tested.
- `tests.yml` tests each stack on the Python it runs on:
  - `pytest`: the pipelines on 3.10, with the daily gate's stack.
  - `dashboard-cloud`: `requirements.txt` alone on 3.14, as Cloud installs it, with the AppTest suite.
  - `monthly-imports`: the real Gmail, PDF and Excel stack on 3.12.
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
- Test each dependency stack on the Python it runs on. One requirements file shared by the 3.10 CI and the 3.14 Cloud app would have broken the Cloud redeploy and failed every Dependabot PR.
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
