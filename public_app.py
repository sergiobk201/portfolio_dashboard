"""
public_app.py
─────────────
Public Streamlit dashboard: performance, allocation and income of the
portfolio. Percentages only — nominal values are never sent to the browser.

Every number comes from pipelines/dashboard_metrics.py (unit-tested); this
file only loads data and renders it. Holdings use the same engine as the TWR
pipeline, so splits and bonus issues are applied identically everywhere.

Secrets: Streamlit Cloud ``st.secrets`` first, then environment / ``.env``
(keys: host, port, dbname, user, password).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import psycopg2
import psycopg2.extensions
import requests
import streamlit as st
import yfinance as yf
from dotenv import load_dotenv

try:  # yfinance ≥ 0.2.50 exposes a typed exception hierarchy
    from yfinance.exceptions import YFException
except ImportError:  # pragma: no cover - older yfinance
    YFException = RuntimeError  # type: ignore[misc,assignment]

ROOT = Path(__file__).resolve().parent
PIPELINES = str(ROOT / "pipelines")
if PIPELINES not in sys.path:  # Streamlit re-executes this file on every rerun
    sys.path.append(PIPELINES)
import dashboard_metrics as dm  # noqa: E402

load_dotenv(ROOT / ".env")  # local runs only; Streamlit Cloud uses st.secrets
log = logging.getLogger("public_app")

SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
SGS_CDI, SGS_PTAX = 12, 1  # CDI (% per business day); USD/BRL PTAX sell rate
SGS_MAX_SPAN = timedelta(days=3650)  # BCB caps daily-series queries at 10 years
HTTP_TIMEOUT_S = 15
# requests and curl_cffi (yfinance) network errors both subclass OSError.
FETCH_ERRORS = (OSError, ValueError, KeyError, TypeError, YFException)

SURFACE, GRID, TEXT, MUTED = "#1a1d24", "#2a2e38", "#e5e7eb", "#a0a4ab"
# Dark categorical steps, validated on SURFACE (CVD-safe in this order).
PALETTE = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
PORTFOLIO_COLOR, SPY_COLOR, CDI_COLOR = PALETTE[0], PALETTE[1], MUTED  # CDI = neutral reference

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


# ─────────────────────────────────────────────────────────────
# DATABASE ACCESS (read-only)
# ─────────────────────────────────────────────────────────────
def get_secret(key: str) -> str | None:
    """Streamlit Cloud secret, else environment variable (None if neither)."""
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except FileNotFoundError:  # no secrets.toml; StreamlitSecretNotFoundError subclasses it
        pass
    return os.getenv(key)


@st.cache_resource(show_spinner=False)
def get_conn() -> psycopg2.extensions.connection:
    """One read-only connection per app process."""
    conn = psycopg2.connect(
        host=get_secret("host"),
        port=get_secret("port"),
        dbname=get_secret("dbname"),
        user=get_secret("user"),
        password=get_secret("password"),
        sslmode=get_secret("DB_SSLMODE") or "require",  # same override as pipelines/db.py
        connect_timeout=10,
    )
    # Emitted as BEGIN READ ONLY per transaction (not a session SET), so it is
    # safe behind a transaction pooler: the public app can never write.
    conn.set_session(readonly=True)
    return conn


def _fetch(conn: psycopg2.extensions.connection, sql: str, params: tuple) -> pd.DataFrame:
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
    finally:
        if not conn.closed:
            conn.rollback()  # end the read-only transaction; never idle in transaction
    return pd.DataFrame(rows, columns=columns)


def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """SELECT → DataFrame (columns kept when empty); one reconnect if the pooler dropped us."""
    conn = get_conn()
    try:
        return _fetch(conn, sql, params)
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        conn.close()  # no-op when already closed
        get_conn.clear()
        return _fetch(get_conn(), sql, params)


def _typed(df: pd.DataFrame, dates: tuple[str, ...] = (), numbers: tuple[str, ...] = ()) -> pd.DataFrame:
    for col in dates:
        df[col] = pd.to_datetime(df[col])
    for col in numbers:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return df


# ─────────────────────────────────────────────────────────────
# DATA LOADERS (cached)
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def load_performance() -> pd.DataFrame:
    """Returns only — the public app never reads nominal portfolio values."""
    sql = "SELECT date, daily_return, twr, is_trading_day FROM portfolio_performance ORDER BY date"
    return _typed(run_query(sql), ("date",), ("daily_return", "twr"))


@st.cache_data(ttl=300, show_spinner=False)
def load_prices() -> pd.DataFrame:
    """Raw closes (no-trade markers carry the last close, corporate-action adjusted)."""
    sql = "SELECT date, ticker, price FROM prices ORDER BY ticker, date"
    return _typed(run_query(sql), ("date",), ("price",))


@st.cache_data(ttl=300, show_spinner=False)
def load_trades() -> pd.DataFrame:
    sql = "SELECT date, side, ticker, quantity, price, total, fee, portfolio FROM trades ORDER BY date"
    return _typed(run_query(sql), ("date",), ("quantity", "price", "total", "fee"))


@st.cache_data(ttl=300, show_spinner=False)
def load_dividends() -> pd.DataFrame:
    sql = (
        "SELECT dividend_date, ticker, type, dividend, total, quantity, portfolio "
        "FROM dividends ORDER BY dividend_date"
    )
    return _typed(run_query(sql), ("dividend_date",), ("dividend", "total", "quantity"))


@st.cache_data(ttl=300, show_spinner=False)
def load_corporate_actions() -> pd.DataFrame:
    sql = "SELECT ticker, ex_date, ratio FROM corporate_actions ORDER BY ex_date"
    return _typed(run_query(sql), ("ex_date",), ("ratio",))


@st.cache_data(ttl=3600, show_spinner=False)
def load_assets() -> pd.DataFrame:
    return run_query("SELECT ticker, sector FROM assets")


@st.cache_data(ttl=1800, show_spinner=False)
def load_benchmark() -> pd.Series:
    """Raw SPY closes (USD) per NYSE session."""
    df = _typed(run_query("SELECT date, price FROM benchmark ORDER BY date"), ("date",), ("price",))
    return pd.Series(df["price"].to_numpy(dtype=float), index=pd.DatetimeIndex(df["date"])).dropna()


# ─────────────────────────────────────────────────────────────
# REFERENCE DATA (BCB, Yahoo) — successes cached for hours, failures for 10 min
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _sgs(code: int, start: date, end: date) -> pd.Series:
    start = max(start, end - SGS_MAX_SPAN)
    resp = requests.get(
        SGS_URL.format(code=code),
        params={"formato": "json", "dataInicial": f"{start:%d/%m/%Y}", "dataFinal": f"{end:%d/%m/%Y}"},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return dm.parse_sgs(resp.json())


@st.cache_data(ttl=600, show_spinner=False)
def fetch_sgs(code: int, start: date, end: date) -> pd.Series:
    """BCB SGS series; empty on failure (logged, shown as a fallback label)."""
    try:
        return _sgs(code, start, end)
    except FETCH_ERRORS as exc:
        log.warning("BCB SGS %s unavailable: %r", code, exc)
        return dm.empty_series()


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def _spy_dividends() -> pd.Series:
    raw = yf.Ticker("SPY").dividends
    if raw is None or raw.empty:
        raise ValueError("Yahoo returned no SPY dividends")
    idx = pd.DatetimeIndex(raw.index)
    idx = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()  # ex-dates
    return pd.Series(raw.to_numpy(dtype=float), index=idx)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_spy_dividends() -> pd.Series:
    """SPY cash dividends by ex-date; empty on failure (benchmark falls back to price)."""
    try:
        return _spy_dividends()
    except FETCH_ERRORS as exc:
        log.warning("SPY dividends unavailable: %r", exc)
        return dm.empty_series()


# ─────────────────────────────────────────────────────────────
# DATABASE LOAD SEQUENCE
# ─────────────────────────────────────────────────────────────
try:
    perf = load_performance()
    prices = load_prices()
    trades = load_trades()
    dividends = load_dividends()
    actions = load_corporate_actions()
    assets = load_assets()
    spy_close = load_benchmark()
except psycopg2.Error as exc:
    log.exception("database load failed")
    st.error("❌ Portfolio database unavailable — please retry in a minute.")
    st.caption(type(exc).__name__)
    st.stop()

if perf.empty:
    st.info("No performance history yet — the daily pipeline has not produced data.")
    st.stop()

latest = perf["date"].max()
ystart = pd.Timestamp(latest.year, 1, 1)  # YTD of the data, not of the server clock
first_needed = min(perf["date"].min(), spy_close.index.min()) if not spy_close.empty else perf["date"].min()
ref_start, ref_end = (first_needed - pd.Timedelta(days=10)).date(), latest.date()


def upto(series: pd.Series) -> pd.Series:
    """Clip a level series at the latest portfolio date (apples-to-apples)."""
    return series[series.index <= latest]


# Portfolio, benchmark and risk-free levels (growth of 1).
twr_level = pd.Series(1.0 + perf["twr"].to_numpy(dtype=float), index=pd.DatetimeIndex(perf["date"]))
cdi_level = upto(dm.accrual_index(fetch_sgs(SGS_CDI, ref_start, ref_end)))
ptax = fetch_sgs(SGS_PTAX, ref_start, ref_end)
spy_divs = fetch_spy_dividends()
spy_usd = dm.total_return_index(spy_close, spy_divs) if not spy_close.empty else dm.empty_series()
spy_level = upto(dm.to_brl(spy_usd, ptax) if not ptax.empty else spy_usd)
spy_label = f"SPY {'TR' if not spy_divs.empty else 'price'} ({'BRL' if not ptax.empty else 'USD'})"

# ─────────────────────────────────────────────────────────────
# DASHBOARD HEADER
# ─────────────────────────────────────────────────────────────
st.markdown("<h1 class='dash-title'>SERGIO'S PORTFOLIO</h1>", unsafe_allow_html=True)
st.markdown(
    f"<div class='dash-caption'>Last updated: <b>{latest:%Y-%m-%d}</b> "
    f"&nbsp;|&nbsp; Reporting: <b>Net Performance & Allocation</b></div>",
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────────────────────
# GLOBAL FILTERS (Ticker, Portfolio, and Date Range)
# ─────────────────────────────────────────────────────────────
st.markdown("<div style='margin-top:15px;'></div>", unsafe_allow_html=True)
fc1, fc2, fc_spacer = st.columns([2, 1, 2])
with fc1:
    all_tickers = sorted(trades["ticker"].dropna().unique().tolist())
    all_pfs = sorted(trades["portfolio"].dropna().unique().tolist())
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
    d_start = (latest - timedelta(days=365)).date()
    d_end = latest.date()
    dr = st.date_input(
        "📅 RANGE",
        value=(d_start, d_end),
        label_visibility="collapsed",
        format="DD/MM/YYYY",
    )

if isinstance(dr, tuple) and len(dr) == 2:
    range_start, range_end = pd.Timestamp(dr[0]), pd.Timestamp(dr[1])
else:  # mid-selection: one date picked so far
    range_start, range_end = pd.Timestamp(d_start), pd.Timestamp(d_end)

port_f = sel_pfs or None
tick_f = sel_tks or None

# ─────────────────────────────────────────────────────────────
# KPI BLOCK CALCULATIONS
# ─────────────────────────────────────────────────────────────
ytd_pct = dm.change_since(twr_level, ystart)
spy_ytd_pct = dm.change_since(spy_level, ystart)
cdi_ytd_pct = dm.change_since(cdi_level, ystart)

sessions = dm.session_returns(perf)  # fill days folded into the preceding B3 session
last_session = sessions.index[-1] if not sessions.empty else latest
daily_ret_pct = float(sessions.iloc[-1]) * 100.0 if not sessions.empty else 0.0
rf_per_session = dm.period_returns(cdi_level, sessions.index) if not cdi_level.empty else None
stats = dm.risk_stats(sessions[sessions.index >= ystart], rf_per_session)
prior_sessions = sessions.index[sessions.index < ystart]
base_session = prior_sessions[-1] if len(prior_sessions) else None  # last B3 close before Jan 1

adjusted = dm.adjusted_prices(prices, actions)  # today's share basis
ytd_by_ticker = dm.ytd_changes(adjusted, ystart, base_session)
held = dm.holdings(trades, actions, adjusted, port_f, tick_f)
scorecard = dm.segment_scorecard(trades, actions, adjusted, dividends, ystart, tick_f, base_session=base_session)

held_ytd = ytd_by_ticker.reindex(held["ticker"]).dropna()
top_tk, top_tk_pct = (held_ytd.idxmax(), float(held_ytd.max())) if not held_ytd.empty else (None, 0.0)
if not scorecard.empty:
    best = scorecard.loc[scorecard["YTD Change"].idxmax()]
    top_pf_name, top_pf_pct = str(best["Portfolio"]), float(best["YTD Change"])
else:
    top_pf_name, top_pf_pct = None, 0.0


def arrow_cls(x: float) -> tuple[str, str]:
    return ("▲", "up") if x >= 0 else ("▼", "down")


def pct(x: float, digits: int = 1) -> str:
    return "—" if pd.isna(x) else f"{x:+.{digits}f}%"


# ─────────────────────────────────────────────────────────────
# KPI ROW RENDER
# ─────────────────────────────────────────────────────────────
k1, k2, k3, k4 = st.columns(4)

with k1:
    a_y, c_y = arrow_cls(ytd_pct)
    st.markdown(
        f"""
        <div class='card'>
            <div class='card-title'>Portfolio Return (YTD)</div>
            <div class='kpi-value {c_y}'>{pct(ytd_pct)}</div>
            <div class='kpi-sub'>vs. {pct(spy_ytd_pct)} {spy_label} · {pct(cdi_ytd_pct)} CDI</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with k2:
    a_t, c_t = arrow_cls(daily_ret_pct)
    st.markdown(
        f"""
        <div class='card'>
            <div class='card-title'>Daily Performance</div>
            <div class='kpi-value {c_t}' style='margin-top:0.9rem;'>
                {a_t} {daily_ret_pct:+.2f}%
            </div>
            <div class='kpi-sub'>B3 session {last_session:%d %b %Y}</div>
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
    rf_note = "rf = CDI" if rf_per_session is not None else "rf = 0 (CDI unavailable)"
    st.markdown(
        f"""
        <div class='card'>
            <div class='card-title'>Portfolio Stats (YTD)</div>
            <div class='kpi-line' style='margin-top:0.7rem;'>Vol: <b>{stats['vol']:.1f}%</b></div>
            <div class='kpi-line'>Sharpe: <b>{stats['sharpe']:.2f}</b></div>
            <div class='kpi-line'>Sortino: <b>{stats['sortino']:.2f}</b></div>
            <div class='kpi-sub'>{stats['sessions']} B3 sessions · {rf_note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("<br>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PERFORMANCE, SCORECARD, & ALLOCATION ROW RENDER
# ─────────────────────────────────────────────────────────────
r2c1, r2c2, r2c3 = st.columns([2.2, 2.0, 1.4])
CHART_LAYOUT = dict(template="plotly_dark", plot_bgcolor=SURFACE, paper_bgcolor=SURFACE)

with r2c1:
    st.markdown("<div class='section-h'>Portfolio Performance</div>", unsafe_allow_html=True)
    grid = twr_level[(twr_level.index >= range_start) & (twr_level.index <= range_end)].index
    lines = [
        ("Portfolio", twr_level, PORTFOLIO_COLOR, "solid"),
        (spy_label, spy_level, SPY_COLOR, "solid"),
        ("CDI", cdi_level, CDI_COLOR, "dash"),
    ]
    series = {
        name: (dm.rebased(level.asof(grid), grid[0], grid[-1]), color, dash)
        for name, level, color, dash in lines
        if len(grid) > 1 and not level.empty
    }
    series = {k: v for k, v in series.items() if not v[0].empty}
    if "Portfolio" in series:
        fig = go.Figure()
        for name, (s, color, dash) in series.items():
            fig.add_trace(
                go.Scatter(
                    x=s.index,
                    y=s.to_numpy(),
                    mode="lines",
                    name=name,
                    line=dict(color=color, width=2, dash=dash),
                    hovertemplate="%{y:+.1f}%",
                )
            )
        # End-of-line direct labels (text ink, not series color), de-collided.
        ends = {name: float(s.iloc[-1]) for name, (s, _, _) in series.items()}
        values = pd.concat([s for s, _, _ in series.values()])
        gap = max(float(values.max() - values.min()) * 0.07, 0.5)
        for name, y in dm.spread_labels(ends, gap).items():
            fig.add_annotation(
                x=series[name][0].index[-1],
                y=y,
                text=f"{name} {ends[name]:+.1f}%",
                xanchor="left",
                xshift=6,
                showarrow=False,
                font=dict(color=TEXT, size=11),
            )
        fig.update_layout(
            **CHART_LAYOUT,
            height=320,
            hovermode="x unified",
            margin=dict(l=20, r=130, t=10, b=80),
            xaxis=dict(title="Date", gridcolor=GRID),
            yaxis=dict(title="Return (%)", gridcolor=GRID, zerolinecolor=GRID),
            legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5, bgcolor="rgba(0,0,0,0)"),
        )
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    else:
        st.info("No TWR points in selected range.")

with r2c2:
    st.markdown("<div class='section-h'>Segment Scorecard (YTD)</div>", unsafe_allow_html=True)
    if not scorecard.empty:
        disp = scorecard.copy()
        for col in ("YTD Change", "DY %", "Weight %"):
            disp[col] = disp[col].map(lambda x: f"{x:.1f} %")
        st.dataframe(disp, hide_index=True, width="stretch", height=260)
        st.caption("YTD Change: current holdings as a Jan 1 basket (Σ value ÷ Σ Jan 1 value − 1) · DY: YTD income ÷ segment value")
    else:
        st.info("No segment data.")


def sector_weights(h: pd.DataFrame) -> pd.Series:
    """Holding value per sector (server-side only; the chart receives weights)."""
    m = h.merge(assets, on="ticker", how="left")
    return m.assign(sector=m["sector"].fillna("Unknown")).groupby("sector")["value"].sum()


with r2c3:
    st.markdown("<div class='section-h'>Sector Allocation</div>", unsafe_allow_html=True)
    view = sector_weights(held) if not held.empty else pd.Series(dtype=float)
    view = view[view > 0]
    if not view.empty:
        # Colors follow the sector, fixed by the unfiltered portfolio: filters never repaint.
        base = dm.fold_other(sector_weights(dm.holdings(trades, actions, adjusted)), len(PALETTE))
        color_of = dict(zip(base.index, PALETTE, strict=False)) | {"Other": MUTED}  # ≤ 8 slots; tail neutral
        view = view.groupby(view.index.where(view.index.isin(base.index), "Other")).sum()
        # 1 dp like the tables: exact weights plus public closes give away share counts.
        view = (view / view.sum() * 100.0).sort_values(ascending=False).round(1)
        view = pd.concat([view.drop("Other", errors="ignore"), view.reindex(["Other"]).dropna()])  # tail last
        fig_d = go.Figure(
            go.Pie(
                labels=view.index,
                values=view.to_numpy(),
                hole=0.6,
                sort=False,
                textinfo="none",
                marker=dict(colors=[color_of.get(s, MUTED) for s in view.index], line=dict(color=SURFACE, width=2)),
                hovertemplate="%{label}: %{value:.1f}%<extra></extra>",
            )
        )
        fig_d.update_layout(
            **CHART_LAYOUT,
            height=300,
            margin=dict(l=10, r=10, t=10, b=10),
            legend=dict(orientation="v", y=0.5, x=1.02),
        )
        st.plotly_chart(fig_d, width="stretch", config={"displayModeBar": False})
    else:
        st.info("No holdings / sector data.")

st.markdown("<br>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# HOLDINGS & DETAILED ACTIVITY ROW RENDER
# ─────────────────────────────────────────────────────────────
r3c1, r3c2 = st.columns([1.3, 2.3])

with r3c1:
    st.markdown("<div class='section-h'>Top Holdings</div>", unsafe_allow_html=True)
    if not held.empty:
        h = held.assign(weight=held["value"] / held["value"].sum() * 100.0, ytd=held["ticker"].map(ytd_by_ticker))
        h = h.sort_values("value", ascending=False).head(10)
        disp = pd.DataFrame(
            {
                "Ticker": h["ticker"].to_numpy(),
                "Weight %": h["weight"].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "—").to_numpy(),
                "Change % (YTD)": h["ytd"]
                .map(lambda x: f"{'▲' if x >= 0 else '▼'} {x:+.1f}%" if pd.notna(x) else "—")
                .to_numpy(),
            }
        )
        st.dataframe(disp, hide_index=True, width="stretch", height=320)
    else:
        st.info("No holdings to show.")

with r3c2:
    with st.expander("Income & Activity", expanded=True):
        tab_div, tab_trd = st.tabs(["Dividends", "Last Trades"])

        with tab_div:
            d = dm.income_events(dividends).sort_values("dividend_date", ascending=False)
            if port_f:
                d = d[d["portfolio"].isin(port_f)]
            if tick_f:
                d = d[d["ticker"].isin(tick_f)]
            d = d.head(10)
            if d.empty:
                st.info("No dividends match filters." if not dividends.empty else "No dividend data.")
            else:
                # Per-event yield on the latest close, split-restated to today's share basis
                # (whole ledger: a row's quantity dates its record day against the splits).
                dy = dm.dividend_yields(d, adjusted, actions, trades)
                disp_d = pd.DataFrame(
                    {
                        "Date": d["dividend_date"].dt.strftime("%Y-%m-%d"),
                        "Ticker": d["ticker"],
                        "Type": d["type"],
                        "DY%": dy.map(lambda x: f"{x:.2f}%" if pd.notna(x) else "—"),
                    }
                )
                st.dataframe(disp_d, hide_index=True, width="stretch")

        with tab_trd:
            t = trades.sort_values("date", ascending=False, kind="mergesort")
            if port_f:
                t = t[t["portfolio"].isin(port_f)]
            if tick_f:
                t = t[t["ticker"].isin(tick_f)]
            t = t.head(10)
            if t.empty:
                st.info("No trades match filters." if not trades.empty else "No trade data.")
            else:
                disp_t = pd.DataFrame(
                    {
                        "Date": t["date"].dt.strftime("%Y-%m-%d").to_numpy(),
                        "Ticker": t["ticker"].to_numpy(),
                        "Side": t["side"].fillna("—").to_numpy(),
                        "Portfolio": t["portfolio"].fillna("—").to_numpy(),
                    }
                )
                st.dataframe(disp_t, hide_index=True, width="stretch")
