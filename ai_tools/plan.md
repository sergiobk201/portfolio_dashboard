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
- [X] Integrate 13-week T-bill yield (^IRX) as the risk-free rate proxy.
- [X] Consolidate KPI tiles for cleaner benchmark comparison.

### [ ] Phase 4: Investor-Ready Dashboard (Public Mode)
- [ ] Implement "Public Mode" toggle to mask all absolute BRL (R$) amounts.
- [ ] Pivot "Top Holdings" and "Trades" to focus on % Weights and Units.
- [ ] Standardize metrics to TWR, Dividend Yield %, and Relative Contribution.
- [ ] Ensure no nominal currency figures are inferable from the UI.

## 🚀 Next Step
- **Implement the "Public Mode" logic in `app.py` to support the Base 1000 re-basing.**
