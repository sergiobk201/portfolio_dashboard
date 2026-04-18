import os
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
import psycopg2.extras
import streamlit as st
import yfinance as yf

# ─────────────────────────────────────────────────────────────
# PAGE CONFIGURATION
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sergio's Portfolio",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────
# CUSTOM CSS STYLING
# ─────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
    /* Expand container to maximize screen real estate for financial charts */
    .block-container {padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1500px;}

    /* Custom dashboard header styling */
    .dash-title {
        color: #4a90e2; font-weight: 800; font-size: 2.4rem;
        letter-spacing: 1px; margin: 0; line-height: 1.1;
    }
    .dash-caption {color: #a0a4ab; font-size: 0.9rem; margin-top: 4px;}
    .dash-caption b {color: #fafafa;}

    /* Card styling for KPI metrics and sections */
    .card {
        background-color: #1a1d24;
        border: 1px solid #2a2e38;
        border-radius: 10px;
        padding: 1.1rem 1.25rem;
        height: 100%;
    }
    .card-title {
        font-size: 0.95rem; color: #c8ccd4;
        font-weight: 600; margin-bottom: 0.4rem;
    }
    .kpi-value {
        font-size: 2.1rem; font-weight: 700; color: #fafafa;
        line-height: 1.1; margin: 0.3rem 0 0.5rem 0;
    }
    .kpi-sub {font-size: 0.82rem; color: #a0a4ab;}
    .kpi-line {font-size: 0.95rem; color: #e5e7eb; margin-top: 0.35rem;}
    .up {color: #10b981; font-weight: 600;}
    .down {color: #ef4444; font-weight: 600;}

    /* Consistent dark background for Streamlit dataframes */
    .stDataFrame {background-color: #1a1d24;}

    /* Section heading styling */
    .section-h {
        font-size: 1.05rem; font-weight: 600; color: #e5e7eb;
        margin: 0 0 0.6rem 0;
    }

    /* Custom tab appearance in the expander section */
    .stTabs [data-baseweb="tab-list"] {gap: 0;}
    .stTabs [data-baseweb="tab"] {
        background-color: #1a1d24; border-radius: 6px 6px 0 0;
        padding: 6px 18px;
    }
</style>
    """,
    unsafe_allow_html=True,
)

# Load environment variables for database connectivity
load_dotenv()

DB_HOST = os.environ.get("host")
DB_PORT = os.environ.get("port")
DB_NAME = os.environ.get("dbname")
DB_USER = os.environ.get("user")
DB_PASSWORD = os.environ.get("password")


def _conn_params():
    # Helper to consolidate connection parameters
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "dbname": DB_NAME,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "sslmode": "require",
    }


@st.cache_resource
def get_conn():
    # Maintains a singleton database connection to optimize resource usage
    return psycopg2.connect(**_conn_params())


def run_query(sql: str, params=None) -> pd.DataFrame:
    # Executing SQL query and returning results as a pandas DataFrame for vectorized processing
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    return pd.DataFrame([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────
# DATA LOADERS (Cached to improve performance)
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_portfolio_performance() -> pd.DataFrame:
    # Loads historical daily performance metrics (TWR, daily returns, total values)
    df = run_query("SELECT * FROM portfolio_performance ORDER BY date")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for c in [
        "portfolio_value",
        "equity_value",
        "uninvested_cash",
        "total_invested",
        "external_inflow",
        "daily_return",
        "twr",
    ]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=300)
def load_prices() -> pd.DataFrame:
    # Loads historical closing prices for all tickers in the universe
    df = run_query("SELECT date, ticker, price FROM prices ORDER BY date")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    return df


@st.cache_data(ttl=300)
def load_trades() -> pd.DataFrame:
    # Loads full trade history ledger
    df = run_query("SELECT * FROM trades ORDER BY date")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for c in ["quantity", "price", "total", "fee"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=300)
def load_dividends() -> pd.DataFrame:
    # Loads all received dividend payments
    df = run_query("SELECT * FROM dividends ORDER BY dividend_date")
    if df.empty:
        return df
    df["dividend_date"] = pd.to_datetime(df["dividend_date"])
    for c in ["dividend", "total", "quantity"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=3600)
def load_assets() -> pd.DataFrame:
    # Loads asset metadata like sector classifications
    return run_query("SELECT * FROM assets")


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_spy(start: date, end: date) -> pd.DataFrame:
    # Fetches external benchmark (SPY) data for performance comparison
    try:
        spy = yf.download(
            "SPY",
            start=start,
            end=end + timedelta(days=1),
            progress=False,
            auto_adjust=True,
        )
        if spy.empty:
            return pd.DataFrame(columns=["date", "spy_price"])
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = [c[0] for c in spy.columns]
        spy = spy.reset_index()[["Date", "Close"]]
        spy.columns = ["date", "spy_price"]
        spy["date"] = pd.to_datetime(spy["date"])
        return spy
    except Exception as e:
        st.warning(f"SPY fetch failed ({e}); benchmark hidden.")
        return pd.DataFrame(columns=["date", "spy_price"])


# ─────────────────────────────────────────────────────────────
# CORE FINANCIAL CALCULATIONS
# ─────────────────────────────────────────────────────────────
def current_holdings(
    trades_df: pd.DataFrame,
    prices_df: pd.DataFrame,
    portfolio_filter=None,
    ticker_filter=None,
) -> pd.DataFrame:
    """Calculates net holdings per ticker by aggregating trade history and joining with latest prices."""
    if trades_df.empty:
        return pd.DataFrame(columns=["ticker", "quantity", "price", "value"])

    t = trades_df.copy()
    if portfolio_filter:
        t = t[t["portfolio"].isin(portfolio_filter)]
    if ticker_filter:
        t = t[t["ticker"].isin(ticker_filter)]
    if t.empty:
        return pd.DataFrame(columns=["ticker", "quantity", "price", "value"])

    # Calculate net signed quantity based on trade side (BUY vs SELL)
    t["qty_signed"] = t.apply(
        lambda r: (
            r["quantity"]
            if str(r["side"]).strip().upper() in ("BUY", "B", "COMPRA")
            else -r["quantity"]
        ),
        axis=1,
    )
    h = t.groupby("ticker", as_index=False)["qty_signed"].sum()
    h.columns = ["ticker", "quantity"]
    h = h[h["quantity"] > 0]

    if h.empty or prices_df.empty:
        h["price"] = np.nan
        h["value"] = np.nan
        return h

    # Join with the latest price per ticker from the pricing table
    last = (
        prices_df.sort_values("date")
        .groupby("ticker", as_index=False)
        .tail(1)[["ticker", "price"]]
    )
    h = h.merge(last, on="ticker", how="left")
    h["value"] = h["quantity"] * h["price"]
    return h


def ytd_price_change_pct(prices_df: pd.DataFrame, ticker: str) -> float:
    # Calculates the price percentage change from Jan 1st of the current year
    if prices_df.empty:
        return np.nan
    p = prices_df[prices_df["ticker"] == ticker].sort_values("date")
    if p.empty:
        return np.nan
    ystart = pd.Timestamp(date(datetime.now().year, 1, 1))
    p_ytd = p[p["date"] >= ystart]
    # Fallback to the first available price if the asset was acquired after Jan 1st
    start_px = p_ytd.iloc[0]["price"] if not p_ytd.empty else p.iloc[0]["price"]
    end_px = p.iloc[-1]["price"]
    if not start_px or pd.isna(start_px):
        return np.nan
    return (end_px - start_px) / start_px * 100.0


def top_ytd_ticker_performer(prices_df, ticker_filter=None):
    # Identifies the ticker with the highest YTD price performance
    if prices_df.empty:
        return None, 0.0
    tickers = prices_df["ticker"].unique()
    if ticker_filter:
        tickers = [t for t in tickers if t in ticker_filter]

    results = []
    for t in tickers:
        chg = ytd_price_change_pct(prices_df, t)
        if not pd.isna(chg):
            results.append((t, chg))

    if not results:
        return None, 0.0

    results.sort(key=lambda x: x[1], reverse=True)
    return results[0][0], float(results[0][1])


def top_portfolio_performer(trades_df, prices_df, ticker_filter=None):
    # Identifies the portfolio segment with the highest weighted YTD performance
    if trades_df.empty:
        return None, 0.0
    pfs = trades_df["portfolio"].dropna().unique()
    results = []
    for pf in pfs:
        h = current_holdings(trades_df, prices_df, [pf], ticker_filter)
        if h.empty or not h["value"].sum():
            continue
        h = h.copy()
        h["ytd"] = (
            h["ticker"].apply(lambda tk: ytd_price_change_pct(prices_df, tk)).fillna(0)
        )
        val = h["value"].sum()
        w = (h["value"] * h["ytd"]).sum() / val if val else 0
        results.append((pf, w))
    if not results:
        return None, 0.0
    results.sort(key=lambda x: x[1], reverse=True)
    return results[0][0], float(results[0][1])


def segment_performance(
    trades_df, prices_df, dividends_df, ticker_filter=None
) -> pd.DataFrame:
    # Calculates a YTD scorecard (Price Return, Dividend Yield, Allocation) per portfolio segment
    cols = ["Portfolio", "YTD Change", "DY %", "Weight %"]
    if trades_df.empty:
        return pd.DataFrame(columns=cols)
    pfs = trades_df["portfolio"].dropna().unique()
    all_h = current_holdings(trades_df, prices_df, ticker_filter=ticker_filter)
    grand = all_h["value"].sum() if not all_h.empty else 0.0

    rows = []
    ystart = pd.Timestamp(date(datetime.now().year, 1, 1))
    for pf in pfs:
        h = current_holdings(trades_df, prices_df, [pf], ticker_filter)
        if h.empty:
            continue
        val = h["value"].sum()
        h = h.copy()
        h["ytd"] = (
            h["ticker"].apply(lambda tk: ytd_price_change_pct(prices_df, tk)).fillna(0)
        )
        twr = (h["value"] * h["ytd"]).sum() / val if val else 0

        dy = 0.0
        if not dividends_df.empty:
            d = dividends_df[dividends_df["portfolio"] == pf]
            d = d[d["dividend_date"] >= ystart]
            if ticker_filter:
                d = d[d["ticker"].isin(ticker_filter)]
            if not d.empty and val:
                dy = d["total"].sum() / val * 100
        weight = (val / grand * 100) if grand else 0
        rows.append(
            {
                "Portfolio": pf,
                "YTD Change": twr,
                "DY %": dy,
                "Weight %": weight,
            }
        )
    return pd.DataFrame(rows, columns=cols)


# ─────────────────────────────────────────────────────────────
# DATABASE LOAD SEQUENCE
# ─────────────────────────────────────────────────────────────
try:
    perf = load_portfolio_performance()
    prices = load_prices()
    trades = load_trades()
    dividends = load_dividends()
    assets = load_assets()
except Exception as e:
    st.error(f"❌ Database connection failed: {e}")
    st.info("Ensure database environment variables are correctly set.")
    st.stop()

latest_date = (
    perf["date"].max()
    if not perf.empty
    else (prices["date"].max() if not prices.empty else pd.Timestamp.today())
)

# ─────────────────────────────────────────────────────────────
# DASHBOARD HEADER
# ─────────────────────────────────────────────────────────────
st.markdown("<h1 class='dash-title'>SERGIO'S PORTFOLIO</h1>", unsafe_allow_html=True)
st.markdown(
    f"<div class='dash-caption'>Last updated: <b>{latest_date.strftime('%Y-%m-%d')}</b> "
    f"&nbsp;|&nbsp; Currency: <b>BRL</b></div>",
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────────────────────
# GLOBAL FILTERS (Ticker, Portfolio, and Date Range)
# ─────────────────────────────────────────────────────────────
st.markdown("<div style='margin-top:15px;'></div>", unsafe_allow_html=True)
fc1, fc2, fc_spacer = st.columns([2, 1, 2])
with fc1:
    all_tickers = (
        sorted(trades["ticker"].dropna().unique().tolist()) if not trades.empty else []
    )
    all_pfs = (
        sorted(trades["portfolio"].dropna().unique().tolist())
        if not trades.empty
        else []
    )
    options = [f"📁 {p}" for p in all_pfs] + [f"▪ {t}" for t in all_tickers]
    chosen = st.multiselect(
        "flt",
        options=options,
        label_visibility="collapsed",
        placeholder="Search Assets or Portfolios...",
    )
    sel_pfs = [c[2:].strip() for c in chosen if c.startswith("📁")]
    sel_tks = [c[2:].strip() for c in chosen if c.startswith("▪")]

with fc2:
    d_start = (latest_date - timedelta(days=365)).date()
    d_end = latest_date.date()
    dr = st.date_input(
        "📅 RANGE",
        value=(d_start, d_end),
        label_visibility="collapsed",
        format="DD/MM/YYYY",
    )

if isinstance(dr, tuple) and len(dr) == 2:
    range_start, range_end = dr
else:
    range_start, range_end = d_start, d_end

port_f = sel_pfs or None
tick_f = sel_tks or None

# ─────────────────────────────────────────────────────────────
# KPI BLOCK CALCULATIONS
# ─────────────────────────────────────────────────────────────
if not perf.empty:
    p_sorted = perf.sort_values("date")
    latest_row = p_sorted.iloc[-1]
    total_value = float(latest_row.get("portfolio_value") or 0)
    daily_ret_pct = float(latest_row.get("daily_return") or 0)
    # Scale correction if daily_return is stored as a decimal factor
    if abs(daily_ret_pct) < 1 and daily_ret_pct != 0:
        daily_ret_pct *= 100
    # Isolate market movement impact from nominal value changes
    daily_change_brl = total_value * (daily_ret_pct / 100) if total_value else 0

    ystart = pd.Timestamp(date(datetime.now().year, 1, 1))
    p_ytd_df = p_sorted[p_sorted["date"] >= ystart].copy()
    if not p_ytd_df.empty:
        # Calculate YTD Time-Weighted Return (TWR) by compounding daily returns (excluding inflows)
        rets = p_ytd_df["daily_return"].fillna(0).astype(float)
        if rets.abs().max() > 1.0:
            rets = rets / 100.0
        ytd_pct = (np.prod(1 + rets) - 1) * 100
    else:
        ytd_pct = 0
else:
    total_value = daily_change_brl = daily_ret_pct = ytd_pct = 0.0

# Secondary KPI metrics for top-performers
top_tk, top_tk_pct = top_ytd_ticker_performer(prices, tick_f)
top_pf_name, top_pf_pct = top_portfolio_performer(trades, prices, tick_f)

spy_ytd_pct = 0.0
try:
    spy_ytd_df = fetch_spy(date(datetime.now().year, 1, 1), latest_date.date())
    if not spy_ytd_df.empty:
        spy_ytd_pct = float(
            (spy_ytd_df["spy_price"].iloc[-1] - spy_ytd_df["spy_price"].iloc[0])
            / spy_ytd_df["spy_price"].iloc[0]
            * 100
        )
except Exception:
    pass


def arrow_cls(x: float):
    return ("▲", "up") if x >= 0 else ("▼", "down")


# ─────────────────────────────────────────────────────────────
# KPI ROW RENDER
# ─────────────────────────────────────────────────────────────
k1, k2, k3, k4 = st.columns(4)

with k1:
    a_t, c_t = arrow_cls(daily_ret_pct)
    a_y, c_y = arrow_cls(ytd_pct)
    st.markdown(
        f"""
        <div class='card'>
            <div class='card-title'>Total Portfolio Value</div>
            <div class='kpi-value'>R$ {total_value:,.0f}</div>
            <div class='kpi-sub'>
                <span class='{c_t}'>{a_t} {daily_ret_pct:+.1f}% TODAY</span>
                &nbsp;&nbsp;
                <span class='{c_y}'>{a_y} {ytd_pct:+.1f}% YTD</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with k2:
    a_d, c_d = arrow_cls(daily_change_brl)
    st.markdown(
        f"""
        <div class='card'>
            <div class='card-title'>Daily Change</div>
            <div class='kpi-value {c_d}' style='margin-top:0.9rem;'>
                {a_d} {daily_change_brl:+,.0f} BRL
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with k3:
    tk = top_tk or "—"
    tp = top_pf_name or "—"
    a1, c1 = arrow_cls(top_tk_pct)
    a2, c2 = arrow_cls(top_pf_pct)
    st.markdown(
        f"""
        <div class='card'>
            <div class='card-title'>YTD Performance</div>
            <div class='kpi-line'>Top Ticker: <b>{tk}</b>
                &nbsp;|&nbsp;<span class='{c1}'>{a1} {top_tk_pct:+.1f}%</span></div>
            <div class='kpi-line'>Top Portfolio: <b>{tp}</b>
                &nbsp;|&nbsp;<span class='{c2}'>{a2} {top_pf_pct:+.1f}%</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with k4:
    st.markdown(
        f"""
        <div class='card'>
            <div class='card-title'>YTD Return Comparison</div>
            <div class='kpi-value'>{ytd_pct:.0f} %</div>
            <div class='kpi-sub'>vs. {spy_ytd_pct:.0f}% SPY</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("<br>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PERFORMANCE, SCORECARD, & ALLOCATION ROW RENDER
# ─────────────────────────────────────────────────────────────
r2c1, r2c2, r2c3 = st.columns([2.2, 2.0, 1.4])

PORTFOLIO_COLOR = "#1f4e79"
SPY_COLOR = "#5ab4e8"
SECTOR_PALETTE = [
    "#3b82f6",
    "#10b981",
    "#f59e0b",
    "#8b5cf6",
    "#ef4444",
    "#06b6d4",
    "#f97316",
    "#ec4899",
]

with r2c1:
    st.markdown(
        "<div class='section-h'>Portfolio Performance</div>",
        unsafe_allow_html=True,
    )
    if not perf.empty and "twr" in perf.columns:
        mask = (perf["date"].dt.date >= range_start) & (
            perf["date"].dt.date <= range_end
        )
        pr = perf.loc[mask].sort_values("date").copy()
        pr = pr.dropna(subset=["twr"])
        if not pr.empty:
            # Auto-detect TWR scale (decimal vs percent) for consistent charting
            scale = 100 if pr["twr"].abs().max() < 5 else 1
            pr["twr_pct"] = pr["twr"] * scale
            # Re-base performance to 0% at the start of the visible date range
            base_ratio = 1 + pr["twr_pct"].iloc[0] / 100
            pr["twr_norm"] = ((1 + pr["twr_pct"] / 100) / base_ratio - 1) * 100

            spy_df = fetch_spy(range_start, range_end)
            if not spy_df.empty:
                base_spy = spy_df["spy_price"].iloc[0]
                spy_df["spy_ret"] = (spy_df["spy_price"] - base_spy) / base_spy * 100

            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=pr["date"],
                    y=pr["twr_norm"],
                    mode="lines",
                    name="Portfolio",
                    line=dict(color=PORTFOLIO_COLOR, width=3),
                )
            )
            if not spy_df.empty:
                fig.add_trace(
                    go.Scatter(
                        x=spy_df["date"],
                        y=spy_df["spy_ret"],
                        mode="lines",
                        name="SPY",
                        line=dict(color=SPY_COLOR, width=3),
                    )
                )
            fig.update_layout(
                template="plotly_dark",
                plot_bgcolor="#1a1d24",
                paper_bgcolor="#1a1d24",
                height=320,
                margin=dict(l=20, r=10, t=10, b=80),
                xaxis=dict(title="Date", gridcolor="#2a2e38"),
                yaxis=dict(title="Return (%)", gridcolor="#2a2e38"),
                legend=dict(
                    orientation="h",
                    yanchor="top",
                    y=-0.25,
                    xanchor="center",
                    x=0.5,
                    bgcolor="rgba(0,0,0,0)",
                ),
            )
            st.plotly_chart(
                fig, use_container_width=True, config={"displayModeBar": False}
            )
        else:
            st.info("No TWR points in selected range.")
    else:
        st.info("No portfolio performance data.")

with r2c2:
    st.markdown(
        "<div class='section-h'>Portfolio Performance</div>",
        unsafe_allow_html=True,
    )
    seg = segment_performance(trades, prices, dividends, tick_f)
    if not seg.empty:
        disp = seg.copy()
        disp["YTD Change"] = disp["YTD Change"].apply(lambda x: f"{x:.1f} %")
        disp["DY %"] = disp["DY %"].apply(lambda x: f"{x:.1f} %")
        disp["Weight %"] = disp["Weight %"].apply(lambda x: f"{x:.1f} %")
        st.dataframe(disp, hide_index=True, use_container_width=True, height=260)
    else:
        st.info("No segment data.")

with r2c3:
    st.markdown(
        "<div class='section-h'>Sector Allocation</div>", unsafe_allow_html=True
    )
    h_all = current_holdings(trades, prices, port_f, tick_f)
    if not h_all.empty and not assets.empty:
        m = h_all.merge(assets[["ticker", "sector"]], on="ticker", how="left")
        m["sector"] = m["sector"].fillna("Unknown")
        sec = (
            m.groupby("sector", as_index=False)["value"]
            .sum()
            .sort_values("value", ascending=False)
        )
        fig_d = px.pie(
            sec,
            names="sector",
            values="value",
            hole=0.6,
            color_discrete_sequence=SECTOR_PALETTE,
        )
        fig_d.update_traces(textinfo="none", hovertemplate="%{label}: %{percent}")
        fig_d.update_layout(
            template="plotly_dark",
            plot_bgcolor="#1a1d24",
            paper_bgcolor="#1a1d24",
            height=300,
            margin=dict(l=10, r=10, t=10, b=10),
            legend=dict(orientation="v", y=0.5, x=1.02),
        )
        st.plotly_chart(
            fig_d, use_container_width=True, config={"displayModeBar": False}
        )
    else:
        st.info("No holdings / sector data.")

st.markdown("<br>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# HOLDINGS & DETAILED ACTIVITY ROW RENDER
# ─────────────────────────────────────────────────────────────
r3c1, r3c2 = st.columns([1.3, 2.3])

with r3c1:
    st.markdown("<div class='section-h'>Top Holdings</div>", unsafe_allow_html=True)
    h = current_holdings(trades, prices, port_f, tick_f)
    if not h.empty:
        tot_v = h["value"].sum()
        h = h.copy()
        h["Weight %"] = h["value"] / tot_v * 100 if tot_v else 0
        h["Change %"] = h["ticker"].apply(lambda tk: ytd_price_change_pct(prices, tk))
        h = h.sort_values("value", ascending=False).head(10)

        disp = pd.DataFrame(
            {
                "Ticker": h["ticker"].values,
                "Qty.": h["quantity"].apply(lambda x: f"{x:,.0f}").values,
                "Price (R$0.00)": h["price"]
                .apply(lambda x: f"R${x:,.2f}" if pd.notna(x) else "—")
                .values,
                "Value (R$0.00)": h["value"]
                .apply(lambda x: f"R${x:,.2f}" if pd.notna(x) else "—")
                .values,
                "Change %": h["Change %"]
                .apply(
                    lambda x: (
                        f"{'▲' if x >= 0 else '▼'} {x:+.1f}%" if pd.notna(x) else "—"
                    )
                )
                .values,
                "Weight %": h["Weight %"].apply(lambda x: f"{x:.1f}%").values,
            }
        )
        st.dataframe(disp, hide_index=True, use_container_width=True, height=320)
    else:
        st.info("No holdings to show.")

with r3c2:
    with st.expander("Expander", expanded=True):
        tab_div, tab_trd = st.tabs(["Dividends", "Last Trades"])

        with tab_div:
            if not dividends.empty:
                d = dividends.copy().sort_values("dividend_date", ascending=False)
                if port_f:
                    d = d[d["portfolio"].isin(port_f)]
                if tick_f:
                    d = d[d["ticker"].isin(tick_f)]
                d = d.head(10)

                if d.empty:
                    st.info("No dividends match filters.")
                else:
                    # Fetch latest prices for individual DY calculation per event
                    last_px = (
                        prices.sort_values("date")
                        .groupby("ticker")
                        .tail(1)[["ticker", "price"]]
                    )
                    d = d.merge(last_px, on="ticker", how="left")

                    # DY per event adjusted to latest market price
                    d["DY%"] = (d["dividend"] / d["price"]) * 100

                    disp_d = pd.DataFrame(
                        {
                            "Date": d["dividend_date"].dt.strftime("%Y-%m-%d"),
                            "Ticker": d["ticker"],
                            "CF": d["total"].apply(lambda x: f"R${x:,.2f}"),
                            "DY%": d["DY%"].apply(
                                lambda x: f"{x:.2f}%" if pd.notna(x) else "—"
                            ),
                        }
                    )
                    st.dataframe(disp_d, hide_index=True, use_container_width=True)
            else:
                st.info("No dividend data.")

        with tab_trd:
            if not trades.empty:
                t = trades.copy().sort_values("date", ascending=False)
                if port_f:
                    t = t[t["portfolio"].isin(port_f)]
                if tick_f:
                    t = t[t["ticker"].isin(tick_f)]
                t = t.head(10)
                if t.empty:
                    st.info("No trades match filters.")
                else:
                    disp_t = pd.DataFrame(
                        {
                            "Date": t["date"].dt.strftime("%Y-%m-%d").values,
                            "Ticker": t["ticker"].values,
                            "Qty": t["quantity"].apply(lambda x: f"{x:,.0f}").values,
                            "Price": t["price"].apply(lambda x: f"R${x:,.2f}").values,
                            "Portfolio": t["portfolio"].fillna("—").values,
                        }
                    )
                    st.dataframe(disp_t, hide_index=True, use_container_width=True)
            else:
                st.info("No trade data.")
