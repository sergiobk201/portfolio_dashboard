# Project Context: Portfolio Dashboard

## 🎯 Objective
Automated ingestion of dividend, trade, and price data to provide a transparent, investor-ready dashboard for quarterly reporting.

## 🛠 Tech Stack
- **Language:** Python 3.x
- **Database:** Supabase (PostgreSQL)
- **Frontend:** Streamlit (v1 Prototype) -> Dash (Target)
- **Visualization:** Plotly (Interactive financial charts, TWR time series, and asset allocation donuts)
- **Infrastructure:** GitHub Actions (Daily Price & TWR updates)
- **Data Sources:** Gmail Automation (Trade/Dividend notices), Market Data APIs (Prices)

## 🏗 Architectural Decisions
- **Vectorized Operations:** Prioritize `pandas` and `numpy` for financial calculations (TWR, performance metrics).
- **Pipeline Segregation:** Discrete scripts for `dividends`, `prices`, and `trades` to ensure modularity and easier debugging in CI/CD.
- **Supabase Integration:** Centralized storage for historical performance, dividends, and trade ledger.
- **Transparency First:** Designed for quarterly investor reporting, focusing on net performance, yield, and asset allocation.

## 📁 Key Files
- `calculate_daily_twr.py`: Core performance calculation engine.
- `dividends_pipeline.py`: Dividend data extraction and Supabase ingestion.
- `prices_pipeline.py`: Market price updates.
- `gmail_automation.py`: Extraction of trade data from email notifications.
- `.github/workflows/`: Automation for daily data refreshes.
