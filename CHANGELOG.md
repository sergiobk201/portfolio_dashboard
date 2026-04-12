# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]
### Added
- `app.py`: Initial Streamlit implementation with Supabase integration.
- `GEMINI.md`: Project technical context and architectural decisions.
- `plan.md`: Roadmap for investor-ready portfolio dashboard implementation.
- Initialized Project Manager tracking.

### Changed
- Implemented **Total Portfolio Value** KPI card with daily performance and YTD TWR.
- Used **Log-Sum method** for vectorized YTD TWR calculation in `app.py`.
- Selected **Plotly** as the primary visualization library for interactive reporting.

### Reflections & Challenges
- **SQL Data Handling:** Resolved a `NoneType` error caused by malformed SQL syntax and missing single quotes in date filtering.
- **Vectorization:** Ensured `numpy` operations on `daily_return` handles potential `float` vs `Decimal` type mismatches from the database.
- **Layout Fidelity:** Used custom HTML/CSS within Streamlit to achieve the dual-delta layout from the `mockup.png`.
