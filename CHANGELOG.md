# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]
### Added
- `app.py`: Full Streamlit implementation with interactive dashboards.
- **Supabase Integration:** Vectorized data fetching for `portfolio_performance`, `prices`, `trades`, and `dividends`.
- **KPI Engine:** Dynamic calculation of Total Value, Daily Return %, Daily Nominal Change, and YTD TWR.
- **Visualizations:**
  - **Portfolio vs. SPY:** Re-based performance chart using Plotly.
  - **Sector Allocation:** Donut chart using asset metadata.
  - **Segment Scorecard:** Comparative table for portfolio sub-segments.
- **Filtering System:** Multi-select for portfolios/tickers and date range controls.
- **Custom CSS:** Dark-themed institutional layout with KPI card styling.
- `plan.md`: Added Phase 4 for Investor-Ready Dashboard (Public Mode).

### Changed
- Refined TWR logic to handle both decimal factor and percentage formats from database sources.
- Implemented robust error handling for external benchmark (SPY) data fetching via `yfinance`.
- Optimized database connections using `@st.cache_resource` for singleton pooling.

### Reflections & Challenges
- **SQL Data Handling:** Resolved a `NoneType` error caused by malformed SQL syntax and missing single quotes in date filtering.
- **Vectorization:** Ensured `numpy` operations on `daily_return` handles potential `float` vs `Decimal` type mismatches from the database.
- **Layout Fidelity:** Used custom HTML/CSS within Streamlit to achieve the dual-delta layout from the `mockup.png`.
- **Investor Transparency:** Pivoting toward Phase 4 to support "Public Mode" for external reporting, masking nominal currency figures.
