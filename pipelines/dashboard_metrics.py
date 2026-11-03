"""
dashboard_metrics.py
────────────────────
Pure metrics behind the public dashboard. No Streamlit and no I/O: the app
loads tables and market series, this module computes, the tests pin it down.

Share counts always come from ``portfolio_engine.build_positions``, so the
dashboard and the performance pipeline cannot disagree about holdings.

Conventions:
  • ``prices`` holds RAW closes. Comparisons across time (YTD, dividend
    yield) restate them in today's share basis with the cumulative
    corporate-action factor F:  adj(t) = raw(t) × F(t) / F(last).
  • Risk statistics use B3-session returns: calendar fill days are folded
    into the preceding session, so compounding stays exact.
  • The benchmark is SPY total return converted to BRL at PTAX, matching
    the currency and income treatment of the portfolio TWR.
  • The risk-free rate is CDI (BCB SGS 12), accrued with the BCB convention
    (day d's rate earns overnight to the next business day).
"""

from __future__ import annotations

from collections.abc import Collection

import numpy as np
import pandas as pd

from portfolio_engine import (
    FRACTION_INCOME_TYPES,
    build_positions,
    corporate_action_factor,
    normalize_corporate_actions,
    normalize_trades,
)

TRADING_DAYS = 252
HOLDINGS_COLUMNS = ["ticker", "quantity", "price", "value"]
SEGMENT_COLUMNS = ["Portfolio", "YTD Change", "DY %", "Weight %"]


def empty_series() -> pd.Series:
    """Empty float time series (a DatetimeIndex, so date comparisons still work)."""
    return pd.Series(dtype=float, index=pd.DatetimeIndex([]))


def _day(values: pd.Series) -> pd.Series:
    out = pd.to_datetime(values)
    if getattr(out.dt, "tz", None) is not None:
        out = out.dt.tz_localize(None)
    return out.dt.normalize().astype("datetime64[ns]")


def _filter(
    frame: pd.DataFrame, portfolios: Collection[str] | None, tickers: Collection[str] | None
) -> pd.DataFrame:
    out = frame
    if portfolios:
        out = out[out["portfolio"].isin(portfolios)]
    if tickers:
        out = out[out["ticker"].isin(tickers)]
    return out


# ── Prices in today's share basis ────────────────────────────────────────────


def adjusted_prices(prices: pd.DataFrame, corporate_actions: pd.DataFrame | None) -> pd.DataFrame:
    """Raw closes plus ``adj_price`` restated in today's share basis.

    Split/bonus adjusted only (never dividend adjusted): a 1:5 split divides
    every earlier close by 5, exactly like an exchange-adjusted chart.

    Args:
        prices: ``prices`` rows [date, ticker, price, ...].
        corporate_actions: ``corporate_actions`` rows, or None.

    Returns:
        DataFrame[date, ticker, price, adj_price] sorted by ticker, date.
    """
    p = prices[["date", "ticker", "price"]].copy()
    p["date"] = _day(p["date"])
    p["price"] = pd.to_numeric(p["price"], errors="coerce").astype(float)
    p = p.dropna(subset=["price"]).sort_values(["ticker", "date"], kind="mergesort").reset_index(drop=True)
    ca = normalize_corporate_actions(corporate_actions)
    if p.empty or ca.empty:
        return p.assign(adj_price=p["price"])
    days = pd.DatetimeIndex(np.sort(p["date"].unique()))
    tickers = pd.Index(np.sort(p["ticker"].unique()))
    factor = corporate_action_factor(ca, days, tickers).to_numpy()
    rows, cols = days.get_indexer(p["date"]), tickers.get_indexer(p["ticker"])
    return p.assign(adj_price=p["price"].to_numpy() * factor[rows, cols] / factor[-1, cols])


def ytd_changes(adjusted: pd.DataFrame, year_start: pd.Timestamp) -> pd.Series:
    """Per-ticker % change from the last close before ``year_start`` to the latest.

    A ticker with no close before ``year_start`` starts from its first close.

    Args:
        adjusted: Output of :func:`adjusted_prices`.
        year_start: First day of the period (Jan 1 for YTD).

    Returns:
        Series of percentages indexed by ticker.
    """
    if adjusted.empty:
        return pd.Series(dtype=float)
    a = adjusted.sort_values(["ticker", "date"], kind="mergesort")
    before = a[a["date"] < year_start].groupby("ticker")["adj_price"].last()
    first = a.groupby("ticker")["adj_price"].first()
    start = before.reindex(first.index).fillna(first)
    end = a.groupby("ticker")["adj_price"].last()
    return (end / start - 1.0) * 100.0


# ── Holdings and segments ────────────────────────────────────────────────────


def holdings(
    trades: pd.DataFrame,
    corporate_actions: pd.DataFrame | None,
    adjusted: pd.DataFrame,
    portfolios: Collection[str] | None = None,
    tickers: Collection[str] | None = None,
) -> pd.DataFrame:
    """Current share counts (corporate-action aware) valued at the latest close.

    Args:
        trades: ``trades`` rows.
        corporate_actions: ``corporate_actions`` rows, or None.
        adjusted: Output of :func:`adjusted_prices` (latest close per ticker,
            already in today's share basis).
        portfolios: Keep only these portfolio segments.
        tickers: Keep only these tickers.

    Returns:
        DataFrame[ticker, quantity, price, value] for positions > 0.
    """
    t = _filter(trades, portfolios, tickers)
    if t.empty:
        return pd.DataFrame(columns=HOLDINGS_COLUMNS)
    norm = normalize_trades(t)
    as_of = max(norm["date"].max(), adjusted["date"].max()) if not adjusted.empty else norm["date"].max()
    days = pd.date_range(norm["date"].min(), as_of, freq="D")
    positions, _ = build_positions(norm, normalize_corporate_actions(corporate_actions), days)
    qty = positions.iloc[-1]
    qty = qty[qty > 0]
    if qty.empty:
        return pd.DataFrame(columns=HOLDINGS_COLUMNS)
    last = adjusted.groupby("ticker")["adj_price"].last() if not adjusted.empty else pd.Series(dtype=float)
    out = pd.DataFrame({"ticker": qty.index.astype(str), "quantity": qty.to_numpy(dtype=float)})
    out["price"] = out["ticker"].map(last).astype(float)
    out["value"] = out["quantity"] * out["price"]
    return out.reset_index(drop=True)


def income_events(dividends: pd.DataFrame) -> pd.DataFrame:
    """Dividends, JCP and FII income only.

    FRAÇÕES rows are cash for fractional shares B3 auctioned after a split or
    bonus issue — a capital event, not income, so they never enter a yield.
    """
    if dividends.empty or "type" not in dividends.columns:
        return dividends
    kind = dividends["type"].astype(str).str.strip().str.upper()
    return dividends[~kind.isin(FRACTION_INCOME_TYPES)]


def weighted_ytd(held: pd.DataFrame, ytd: pd.Series) -> float:
    """Value-weighted YTD % of a set of holdings (0 when nothing is valued)."""
    total = float(held["value"].sum()) if not held.empty else 0.0
    if not total:
        return 0.0
    return float((held["value"] * held["ticker"].map(ytd).fillna(0.0)).sum() / total)


def segment_scorecard(
    trades: pd.DataFrame,
    corporate_actions: pd.DataFrame | None,
    adjusted: pd.DataFrame,
    dividends: pd.DataFrame,
    year_start: pd.Timestamp,
    tickers: Collection[str] | None = None,
) -> pd.DataFrame:
    """YTD scorecard per portfolio segment: price change, dividend yield, weight.

    Returns:
        DataFrame[Portfolio, YTD Change, DY %, Weight %] (percentages).
    """
    if trades.empty:
        return pd.DataFrame(columns=SEGMENT_COLUMNS)
    ytd = ytd_changes(adjusted, year_start)
    grand = float(holdings(trades, corporate_actions, adjusted, tickers=tickers)["value"].sum())
    income = income_events(dividends)
    if not income.empty:
        income = income.assign(dividend_date=_day(income["dividend_date"]))
    rows = []
    for pf in sorted(trades["portfolio"].dropna().unique()):  # a handful of segments
        held = holdings(trades, corporate_actions, adjusted, portfolios=[pf], tickers=tickers)
        value = float(held["value"].sum()) if not held.empty else 0.0
        if not value:
            continue
        dy = 0.0
        if not income.empty:
            d = _filter(income[income["dividend_date"] >= year_start], [pf], tickers)
            dy = float(pd.to_numeric(d["total"], errors="coerce").sum()) / value * 100.0
        rows.append(
            {
                "Portfolio": pf,
                "YTD Change": weighted_ytd(held, ytd),
                "DY %": dy,
                "Weight %": value / grand * 100.0 if grand else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=SEGMENT_COLUMNS)


def dividend_yields(
    dividends: pd.DataFrame,
    adjusted: pd.DataFrame,
    corporate_actions: pd.DataFrame | None,
) -> pd.Series:
    """Per-event dividend yield (%) against the latest close, in today's share basis.

    The per-share amount is ``total / quantity``: the stored ``dividend``
    column has 2 decimals, which rounds small JCP / FII amounts by up to a
    third (or to zero). It falls back to ``dividend`` when quantity is
    missing. An amount paid before a 1:5 split is divided by 5 so it is
    comparable with the post-split close.
    """
    if dividends.empty or adjusted.empty:
        return pd.Series(np.nan, index=dividends.index, dtype=float)
    last = adjusted.groupby("ticker")["adj_price"].last()
    paid = _day(dividends["dividend_date"])
    per_share = pd.to_numeric(dividends["dividend"], errors="coerce").astype(float)
    if {"total", "quantity"} <= set(dividends.columns):
        qty = pd.to_numeric(dividends["quantity"], errors="coerce").astype(float)
        exact = pd.to_numeric(dividends["total"], errors="coerce").astype(float) / qty.where(qty > 0)
        per_share = exact.fillna(per_share)
    ca = normalize_corporate_actions(corporate_actions)
    ca = ca[ca["ex_date"] <= adjusted["date"].max()]  # same basis as the latest close
    later = pd.Series(1.0, index=dividends.index)
    for ev in ca.itertuples(index=False):  # handful of events
        later[(dividends["ticker"] == ev.ticker) & (paid < ev.ex_date)] *= ev.ratio
    return per_share / later / dividends["ticker"].map(last) * 100.0


# ── Returns and risk ─────────────────────────────────────────────────────────


def session_returns(perf: pd.DataFrame) -> pd.Series:
    """Daily TWR returns compounded into B3-session periods.

    Calendar fill days (weekends, holidays) fold into the preceding session,
    so Π(1 + r) over sessions equals Π(1 + r) over calendar days.

    Args:
        perf: ``portfolio_performance`` rows [date, daily_return, is_trading_day].

    Returns:
        Series of session returns indexed by session date.
    """
    p = perf.sort_values("date")
    is_session = p["is_trading_day"].eq(True).to_numpy()
    key = np.cumsum(is_session)
    keep = key > 0
    growth = (1.0 + pd.to_numeric(p["daily_return"], errors="coerce").fillna(0.0).to_numpy()[keep])
    by_session = pd.Series(growth).groupby(key[keep]).prod()
    dates = pd.DatetimeIndex(_day(p.loc[is_session, "date"]))
    return pd.Series(by_session.to_numpy() - 1.0, index=dates)


def compound(returns: pd.Series) -> float:
    """Π(1 + r) − 1 as a percentage."""
    return float(np.prod(1.0 + returns.to_numpy(dtype=float)) - 1.0) * 100.0


def risk_stats(returns: pd.Series, rf_daily: pd.Series | None) -> dict[str, float]:
    """Annualised volatility (%), Sharpe and Sortino on session returns.

    Args:
        returns: Output of :func:`session_returns` for the period.
        rf_daily: Risk-free return per session (decimal, e.g. CDI accrued
            between consecutive sessions via :func:`period_returns`),
            aligned as-of on the return dates; None or empty = 0.

    Returns:
        {"vol": %, "sharpe": x, "sortino": x, "sessions": n}.
    """
    out = {"vol": 0.0, "sharpe": 0.0, "sortino": 0.0, "sessions": int(len(returns))}
    if len(returns) < 2:
        return out
    rf = 0.0
    if rf_daily is not None and not rf_daily.empty:
        rf = rf_daily.sort_index().asof(returns.index).fillna(0.0)  # as-of: latest known rate
    excess = returns - rf
    out["vol"] = float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS) * 100.0)
    excess_vol = float(excess.std(ddof=1))
    if excess_vol > 0:
        out["sharpe"] = float(excess.mean() / excess_vol * np.sqrt(TRADING_DAYS))
    downside = float(np.sqrt(np.mean(np.minimum(0.0, excess.to_numpy(dtype=float)) ** 2)))
    if downside > 0:
        out["sortino"] = float(excess.mean() / downside * np.sqrt(TRADING_DAYS))
    return out


# ── Benchmark ────────────────────────────────────────────────────────────────


def total_return_index(closes: pd.Series, dividends: pd.Series | None = None) -> pd.Series:
    """Total-return index (start = 1) with each dividend reinvested at its ex-date close.

    Args:
        closes: Raw closes indexed by session date.
        dividends: Cash dividend per share indexed by ex-date, or None.

    Returns:
        Index on the closes' dates.
    """
    c = closes.sort_index().astype(float)
    cash = np.zeros(len(c))
    if dividends is not None and not dividends.empty:
        d = dividends[(dividends.index > c.index[0]) & (dividends.index <= c.index[-1])]
        rows = c.index.searchsorted(d.index)  # ex-date on a non-session → next session
        np.add.at(cash, rows, d.to_numpy(dtype=float))
    growth = (c + cash) / c.shift(1)
    return growth.fillna(1.0).cumprod()


def to_brl(index_usd: pd.Series, usd_brl: pd.Series) -> pd.Series:
    """USD index converted to BRL on a calendar grid (both carried forward)."""
    if index_usd.empty or usd_brl.empty:
        return empty_series()
    days = pd.date_range(index_usd.index.min(), index_usd.index.max(), freq="D")
    fx = usd_brl.sort_index().reindex(days.union(usd_brl.index)).ffill().reindex(days)
    return (index_usd.sort_index().reindex(days).ffill() * fx).dropna()


def rebased(series: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """% change of ``series`` from its first value in [start, end]."""
    s = series[(series.index >= start) & (series.index <= end)].dropna()
    return (s / s.iloc[0] - 1.0) * 100.0 if not s.empty else s


def change_since(level: pd.Series, start: pd.Timestamp) -> float:
    """% change of a level series from its value as of ``start`` to its last value.

    Falls back to the first value when the series begins after ``start``
    (e.g. a portfolio opened mid-year). NaN when the series is empty.
    """
    s = level.dropna().sort_index()
    if s.empty:
        return float("nan")
    base = s.asof(start) if start >= s.index[0] else s.iloc[0]
    return float(s.iloc[-1] / base - 1.0) * 100.0


def period_returns(level: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """Growth of ``level`` between consecutive ``dates`` (as-of), first period = 0."""
    at = level.dropna().sort_index().asof(dates)
    return (at / at.shift(1) - 1.0).fillna(0.0)


# ── Brazilian reference rates (BCB SGS) ──────────────────────────────────────


def parse_sgs(payload: list[dict[str, str]]) -> pd.Series:
    """BCB SGS JSON rows ``[{"data": "dd/mm/yyyy", "valor": "1.23"}]`` → float Series.

    Raises:
        ValueError / KeyError / TypeError on a payload that is not SGS rows
        (BCB answers errors with a JSON object or an HTML page).
    """
    if not payload:
        return empty_series()
    frame = pd.DataFrame(payload)
    index = pd.DatetimeIndex(pd.to_datetime(frame["data"], format="%d/%m/%Y"))
    values = pd.to_numeric(frame["valor"], errors="coerce").to_numpy(dtype=float)
    out = pd.Series(values, index=index).dropna()
    return out[~out.index.duplicated(keep="last")].sort_index()


def accrual_index(rates_pct: pd.Series) -> pd.Series:
    """Growth of 1 at a daily rate quoted in % per business day (CDI).

    The rate published for business day d accrues overnight to the next
    business day, so the level is stamped on d + 1 day: ``asof(t)`` is then
    Π(1 + r_d) over every rate date d < t, and asof(b) / asof(a) is the
    accrual from a's close to b's close — the BCB "CDI acumulado" convention.
    """
    r = rates_pct.dropna().sort_index()
    if r.empty:
        return empty_series()
    level = (1.0 + r / 100.0).cumprod()
    level.index = level.index + pd.Timedelta(days=1)
    start = pd.Series([1.0], index=[r.index[0]])
    return pd.concat([start, level])


# ── Presentation helpers ─────────────────────────────────────────────────────


def fold_other(values: pd.Series, slots: int, other: str = "Other") -> pd.Series:
    """Keep the ``slots - 1`` largest entries and sum the rest into ``other``.

    A categorical palette has a fixed number of validated slots; a 9th
    category never gets a generated hue.
    """
    v = values.sort_values(ascending=False)
    if len(v) <= slots:
        return v
    head = v.iloc[: slots - 1]
    return pd.concat([head, pd.Series({other: float(v.iloc[slots - 1 :].sum())})])


def spread_labels(values: dict[str, float], min_gap: float) -> dict[str, float]:
    """Label y-positions at least ``min_gap`` apart, pushed upward from the lowest."""
    placed: dict[str, float] = {}
    floor = -np.inf
    for name, y in sorted(values.items(), key=lambda kv: kv[1]):
        y = max(y, floor + min_gap)
        placed[name] = y
        floor = y
    return placed
