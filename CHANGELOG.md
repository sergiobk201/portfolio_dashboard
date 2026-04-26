# Changelog

All notable changes to this project will be documented in this file.

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
