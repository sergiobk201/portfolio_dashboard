<!-- Hero -->
<h1 align="center">Portfolio Dashboard</h1>

<p align="center">
  <strong>Professional stock portfolio dashboard for interested investors, friends and family that would like to take a peek at my different portfolios.</strong>
</p>

<p align="center">
  <a href="https://github.com/sergiobk201/portfolio_dashboard/actions/workflows/daily_pipeline.yml"><img src="https://img.shields.io/github/actions/workflow/status/sergiobk201/portfolio_dashboard/daily_pipeline.yml?branch=main&style=flat-square&label=daily%20pipeline" alt="Daily pipeline"></a>
  <a href="https://github.com/sergiobk201/portfolio_dashboard/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/sergiobk201/portfolio_dashboard/tests.yml?branch=main&style=flat-square&label=tests" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/sergiobk201/portfolio_dashboard?style=flat-square" alt="License"></a>
  <a href="https://github.com/sergiobk201/portfolio_dashboard/stargazers"><img src="https://img.shields.io/github/stars/sergiobk201/portfolio_dashboard?style=flat-square" alt="Stars"></a>
  <img src="https://img.shields.io/badge/python-3.10-blue?style=flat-square" alt="Python Version">
</p>

<p align="center">
  <img src="demo.png" alt="Portfolio Dashboard mockup" width="720">
</p>

---

## ✨ Overview

This project is an automated, investor-ready dashboard for tracking and reporting portfolio performance. Trades, dividends and prices come from Gmail notifications and market data sources and land in a central Supabase database. The project computes institutional-grade metrics from them, such as Time-Weighted Return (TWR) and risk-adjusted ratios.

It is built for transparency and a professional look. Trusted parties can follow the portfolio's progress, while daily automated refreshes and validation gates protect data integrity.

## 🚀 Features

- 🔄 **Automated Pipelines**: one daily GitHub Actions run fetches official B3 closes, then the SPY benchmark, then recomputes all performance, gated by the test suite.
- 🏛 **Official Prices**: session closes come straight from B3's COTAHIST files. Yahoo and BRAPI are only labelled fallbacks, and every row records its source.
- 🧾 **Audit-Grade Accounting**: every run rebuilds the full history from raw closes, corporate actions and implied contributions. Validation gates block bad writes.
- 📊 **Quant Metrics**: TWR, XIRR, annualized volatility, Sharpe and Sortino ratios.
- 📉 **Local Risk-Free & BRL Benchmark**: CDI is the risk-free rate, and the benchmark is SPY total return converted to BRL.
- 🎨 **Interactive UI**: Streamlit dashboard with custom CSS and Plotly financial charts.
- 🔒 **Public Mode**: the public app shows percentages only, and nominal values never reach the browser.
- 🧯 **Guarded Repairs**: historical corrections ship as one rehearsed transaction that verifies prod's state before and after the change.
- 🏦 **Supabase Backend**: central PostgreSQL storage for the trade ledger and the performance history.

## 📸 Live Portfolio

> 🔗 **Click here:** (https://barkel-portfolio.streamlit.app)

## 🏁 Quick Start

### 1. Clone the repository
```bash
git clone https://github.com/sergiobk201/portfolio_dashboard.git
cd portfolio_dashboard
```

### 2. Credentials
Copy [`.env.example`](.env.example) to `.env` at the repository root and fill in the database keys (`host`, `port`, `dbname`, `user`, `password`). The dashboard and the pipelines both read this file, and it is git-ignored.

On Streamlit Community Cloud, paste the same keys as TOML under **App settings → Secrets**. Streamlit secrets take precedence over `.env` when both exist.
```toml
host = "<...>.pooler.supabase.com"   # Session pooler (IPv4); db.<ref>.supabase.co is IPv6-only
port = "5432"
dbname = "postgres"
user = "postgres.<project-ref>"
password = "<database password>"
```

> 🔐 **Recommended:** don't give the public app the `postgres` login. [`tools/dashboard_role.sql`](tools/dashboard_role.sql) creates `dashboard_ro`. It can SELECT what the page shows, sees only return columns on `portfolio_performance`, and can never write. Point the two secrets above at it (`user = "dashboard_ro.<project-ref>"`). The pipelines keep their own login.

### 3. Install & Run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run public_app.py
```

## 📦 Installation

### Requirements
- Python: the pipelines run 3.10 (`prices_requirements.txt`), the dashboard 3.11–3.14 (`requirements.txt`; Streamlit Cloud runs 3.14), and the monthly imports 3.10–3.13 (`requirements-monthly.txt`). CI tests each stack on the Python it runs on.
- A Supabase (PostgreSQL) project
- Gmail API OAuth credentials, for ledger ingestion
- Docker and the Supabase CLI, for [repairs](#-repairs) only

The pipelines read the variables listed in [`.env.example`](.env.example). Locally they come from `.env` at the repository root. In Actions they come from repository secrets.

### Pipelines
`.github/workflows/daily_pipeline.yml` runs at 07:30 BRT, with an idempotent retry at 13:30:

| Step | Module | What it does |
|---|---|---|
| gate | `tests/` | Runs the synthetic-data test suite. Any failure blocks every write. |
| prices | `prices_pipeline.py` | Fills every held (ticker, session) gap from B3 COTAHIST, then Yahoo raw, then BRAPI. Marks confirmed no-trade sessions and syncs splits and bonuses. |
| benchmark | `spy_pipeline.py` | Stores SPY closes on NYSE sessions (FMP, then Yahoo raw). |
| performance | `calculate_daily_twr.py` | Rebuilds the whole series from the ledger and writes only the changed rows. |

Shared modules: `portfolio_engine.py` (pure, vectorized accounting), `dashboard_metrics.py` (pure dashboard statistics), `market_data.py` (calendars and providers), `db.py` and `alerts.py` (Telegram).
Ledger ingestion: `gmail_automation.py` reads trades from the notas de corretagem (fee math in `nota_fees.py`), and `dividends_pipeline.py` reads income.

## 🗓 Monthly routine

The daily pipeline runs on its own. Once a month, load the new ledger entries. The trade import looks back 45 days, so a monthly run always overlaps the previous one; after a longer gap, pass `--days N`. Both imports skip rows already stored, so re-running either one is safe.

0. **Environment (once).** `python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements-monthly.txt`. This pins the Gmail, PDF and Excel stack; any Python from 3.10 to 3.13 works.
1. **Credentials.** Put `.env` (template: [`.env.example`](.env.example)) and `credentials.json` (the Google OAuth client) in the repository root. Both files are git-ignored. The first Gmail run opens a browser for consent and caches `pipelines/token.json`.
2. **Trades.** Run `python pipelines/gmail_automation.py`. It imports every Rico nota de corretagem e-mailed in the last 45 days, oldest first. Each nota is opened with `PASSWORD_PDF`, every trade line must reconcile (quantity × price = total), fees come from the nota's cost blocks, and trades already in `trades` are skipped. The run stops at the first nota that fails, names it and exits with code 1; the notas before it stay loaded, so fix the cause and re-run. The PDFs are deleted after each nota unless you pass `--keep-pdfs` (they stay in the git-ignored `attachments/`).
3. **Income.** Run `python pipelines/dividends_pipeline.py` and give it the Rico account-statement export (.xlsx). It loads dividends, JCP, rendimentos and fraction payouts into `dividends`; lines already stored are skipped, so an overlapping statement is safe.
4. **Recompute.** Run `gh workflow run daily_pipeline.yml --repo sergiobk201/portfolio_dashboard`, or wait for the next 07:30 BRT run. Every run rebuilds the whole history from the ledger, so back-dated entries are picked up automatically.
5. **Read the Telegram alerts.**

| Alert | Meaning | Action |
|---|---|---|
| 📝 History revised from *date* | Rows older than the 7-day grace window changed | **Expected after a monthly import** (back-dated trades or income) or a corporate-action insert. Otherwise, find out what changed. |
| 🧾 New corporate actions applied | A split or bonus reported by Yahoo is now applied | Check the ratio and ex-date against the broker's notice. |
| ⚠️ No close for *date* / SPY close missing | No source has that session's bar yet | Nothing to do. The retry fills it, and the previous close is carried meanwhile. The Actions run shows red until then. |
| ⚠️ New data warnings | A stale price, a > 40 % jump with no corporate action, or a fraction with no event | Check for a missed split or bonus (see below). |
| 🔍 Closes differ / Benchmark check | A provider disagrees with a stored row (the stored row is kept) | If it persists, run a [repair](#-repairs). |
| 🚨 Held with no print ever | A held ticker has no close at all, so the TWR job blocks | Check for a ticker rename. Otherwise insert a `manual` price (see below). |
| 🚨 TWR job blocked | A validation gate failed and nothing was written | Fix the ledger row the alert names, then rerun. |
| 🔁 Cumulative TWR crossed zero | Informational | None. |

**Missed bonus or split.** Insert it once. The next run applies it from the ex-date (expect a 📝 alert):
```sql
INSERT INTO public.corporate_actions (ticker, ex_date, ratio, action_type, source, notes)
VALUES ('TICK3', DATE '2027-01-15', 1.1, 'BONUS', 'manual', '10% bonus per broker notice');
-- ratio = new shares per old share: 5 = 1:5 split, 0.1 = 10:1 reverse split, 1.1 = 10% bonus
-- action_type: SPLIT | REVERSE_SPLIT | BONUS
```

**Missing close.** Insert a manual price. An official close replaces a fallback source automatically, but `manual` rows stay as entered:
```sql
INSERT INTO public.prices (ticker, date, price, source) VALUES ('TICK3', DATE '2027-01-15', 12.34, 'manual');
```

**Ledger corrections.** Fix a wrong trade or dividend row directly in the database, one `UPDATE` per id. The next run recomputes everything from the ledger. For bulk or risky corrections, use the rehearsed [repair](#-repairs) flow.

## 🛠 Usage

### Running Automation
Pipelines run in GitHub Actions, but you can also run them by hand from the repository root:
```bash
python pipelines/prices_pipeline.py       # official closes for every held session
python pipelines/spy_pipeline.py          # benchmark
python pipelines/calculate_daily_twr.py   # full recompute → portfolio_performance

# Tests (synthetic data only) and the CI syntax check
pip install -r requirements-dev.txt && python -m pytest
# Dashboard tests (Python 3.11+, the Streamlit Cloud stack)
pip install -r requirements.txt pytest && python -m pytest tests/test_dashboard_metrics.py tests/test_public_app.py
python -m compileall -q pipelines tools public_app.py
```

### Dashboard Customization
The dashboard lives in `public_app.py`, with its statistics in `pipelines/dashboard_metrics.py`. Custom CSS gives it a dark, institutional look.

## 🧯 Repairs

Historical corrections never go straight into prod.

1. `tools/rebuild.sh` snapshots prod (read-only) and loads it into a local replica.
2. It plans the repair with `backfill_history.py` and rehearses it as a single guarded transaction on two fresh replicas.
3. `tools/apply_deploy.sh` applies that transaction. It commits only if prod still matches the snapshot and ends identical to the rehearsal.

```zsh
tools/rebuild.sh [--ledger-fix .repair/fix.sql]     # read-only on prod
tools/apply_deploy.sh .repair/run_<stamp> --yes     # the one prod write
```

The [runbook](tools/README.md) covers setup, what each check proves, failure handling and rollback.

## 🏗 Architecture

The system is designed for modularity and vectorized performance:
- **Ingestion layer:** Python scripts running in GitHub Actions.
- **Persistence layer:** Supabase (PostgreSQL) for all time-series data.
- **Calculation layer:** a NumPy/Pandas engine for TWR and risk metrics.
- **Presentation layer:** Streamlit for interactive reporting.

## 📐 Methodology

- **Valuation**: positions are valued at the raw session close printed by B3, never a dividend-adjusted one. Income enters as cash on its payment date, so it is counted exactly once.
- **Corporate actions**: splits, reverse splits and bonus issues change share counts on the ex-date (`floor(shares × ratio)`; B3 pays the fractions in cash). Closes carried across an ex-date are rescaled.
- **Cash & contributions**: sale proceeds and income fund later buys. A buy beyond the available cash implies an external contribution, with the closed form `I_t = max(0, max_{s≤t} −C_t)` on cumulative net flows.
- **TWR**: daily returns with start-of-day flows, chain-linked as `r_t = V_t / (V_{t−1} + F_t) − 1`. This is identical to a unitised NAV where contributions buy units at the prior close. XIRR is reported alongside as the money-weighted view.
- **Dashboard**: holdings and per-asset YTD are corporate-action aware via the same engine (closes restated in today's share basis, YTD from the last close before Jan 1). Risk statistics use B3-session returns, with fill days folded into the preceding session. Risk-free = CDI (BCB SGS 12), accrued by the BCB convention: day *d*'s rate earns overnight to the next business day. Benchmark = SPY total return (dividends reinvested on the ex-date) converted to BRL at USD/BRL PTAX (BCB SGS 1). The page falls back visibly to SPY price, USD or rf = 0 when a feed is down.
- **Income yield**: per-event DY = `total ÷ quantity` over the latest close, restated for later splits (the stored per-share column has 2 decimals). FRAÇÕES are cash for auctioned fractional shares, a capital event, so they never enter a yield.
- **Public by design**: the app opens a read-only session (`BEGIN READ ONLY`), never queries nominal portfolio values and sends only percentages and weights to the browser.
- **Gates**: an unpriced holding, a negative position or an unknown trade side blocks every write. Stale prices, possible missing splits (a > 40 % jump in the split-continuous series) and fractions with no recorded event raise alerts.
- **Provenance**: every price row carries its `source` (`b3_cotahist`, `b3_no_trade`, `yahoo_raw`, `brapi`, `legacy` or `manual`). Fallback rows are upgraded to the official close automatically.
- **Known limits**: withdrawals are not modelled (the ledger has none), and the IRPF cost basis does not include the *custo atribuído* of bonus shares.

## 🗺 Roadmap

- [x] Phase 1: Research & Setup
- [x] Phase 2: Streamlit Prototype
- [x] Phase 3: Reporting & Polish (Sharpe/Sortino)
- [x] Phase 4: Full "Public Mode" implementation
- [x] Phase 5: Nov 2026 rebuild (official closes, corporate actions, full-recompute TWR, guarded repairs)

## 📝 Changelog

- **v0.5.0**: rebuilt the public dashboard on the engine (corporate-action-aware holdings and YTD, CDI risk-free, SPY total return in BRL, exact DY). It is read-only, weights-only and AppTest-covered. The monthly imports are idempotent (every nota in a 45-day window, reconciled line by line), and both the dashboard and the imports have pinned stacks.
- **v0.4.0**: rebuilt the performance engine (full recompute, corporate actions, official B3 closes, validation gates), consolidated the daily workflows, and added the test suite and a guarded historical backfill.
- **v0.3.0**: added advanced risk metrics (Sharpe, Sortino) and ^IRX integration.
- **v0.2.0**: implemented full Supabase integration and daily GitHub Actions.
- **v0.1.0**: initial Streamlit prototype with basic TWR tracking.

See [CHANGELOG.md](ai_tools/CHANGELOG.md) for the full history.

## 🤝 Contributing

Contributions are welcome. Feel free to open an issue or submit a pull request.

## 📄 License

[MIT](LICENSE) © 2026 Sergio B.K.

## 🙏 Acknowledgments

- Market data from [B3](https://www.b3.com.br) (COTAHIST), [Banco Central do Brasil](https://www3.bcb.gov.br/sgspub) (SGS), [Financial Modeling Prep](https://financialmodelingprep.com), [Brapi](https://brapi.dev) and [yfinance](https://github.com/ranaroussi/yfinance).
- UI built with [Streamlit](https://streamlit.io).
- Calculations follow institutional TWR standards and common risk-adjusted metrics from asset management.
