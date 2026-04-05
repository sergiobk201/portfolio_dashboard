# Portfolio Dashboard: Implementation Plan

## 🎯 Objective
Build a professional, investor-grade dashboard for transparent quarterly reporting, ingesting dividends, trades, and prices from Supabase into a Python-based frontend.

## 📅 Timeline: April 2026
- **Phase 1: Research & Setup (Days 1-2):** Map database schema and select frontend components.
- **Phase 2: Streamlit Prototype (Days 3-7):** Functional v1 dashboard.
- **Phase 3: Refinement & Dash Migration (Days 8-14):** Advanced styling and interactive reporting features.

## 🗺 Roadmap

### [ ] Phase 1: Preparation
- [X] Verify Supabase schema for `dividends`, `trades`, and `daily_prices`.
- [X] Confirm TWR (Time-Weighted Return) calculation consistency.
- [X] Research Streamlit-friendly financial visualization libraries (Selected: Plotly).

### [ ] Phase 2: Streamlit v1 Implementation
- [ ] Implement data fetching logic for Supabase (using `supabase-py`).
- [ ] Build "Portfolio Overview" (Total Value, TWR, Asset Allocation).
- [ ] Build "Dividend Tracker" (Annual Yield, Monthly Income, Forecast).
- [ ] Build "Trade History" (Recent Activity, P&L by position).
- [ ] Add sidebar filters for Date Range (Quarterly/Yearly) and Ticker.

### [ ] Phase 3: Reporting & Polish
- [ ] Generate a "Quarterly PDF Report" export feature.
- [ ] Implement interactive Plotly charts for cumulative returns vs. benchmark (S&P 500).
- [ ] Style the app with custom CSS to match an institutional/professional look.
- [ ] (Optional) Begin migration of complex components to Dash for enhanced interactivity.

## 🚀 Next Step
- **Review Supabase connection credentials and verify the current state of data in the tables.**
