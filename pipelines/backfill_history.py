"""
backfill_history.py
───────────────────
Re-runnable repair of the price history and full rebuild of the derived
tables from the exchange's own files. Dry run by default — nothing is written
without --apply.

    python pipelines/backfill_history.py --cache-dir ~/.cache/portfolio        # plan + validation
    python pipelines/backfill_history.py --cache-dir ~/.cache/portfolio --report recon.md
    python pipelines/backfill_history.py --cache-dir ~/.cache/portfolio --emit-sql repair.sql
    python pipelines/backfill_history.py --cache-dir ~/.cache/portfolio --apply

The plan
  prices      Every stored close on a B3 session is aligned to the official
              COTAHIST close (the 2026-03-08 bulk load was dividend-adjusted;
              ~0.2% of later daily rows were wrong too). Rows on non-sessions
              are deleted; held sessions with no row get the official close;
              held sessions with no trades get a 'b3_no_trade' marker;
              non-held rows with no official print (provider artefacts) go.
  benchmark   Rebuilt from Yahoo SPY raw closes on NYSE sessions.
  corporate_actions
              Yahoo split/bonus events for every traded ticker plus
              MANUAL_CORPORATE_ACTIONS (events Yahoo omits). Existing rows kept.
  portfolio_performance
              Full engine recompute — the same code path as the daily job.

Every write runs in one transaction, after point-in-time copies of the four
tables into the ``backup`` schema. --emit-sql writes exactly the statements
--apply would execute, for review or for running through another client.

Requires sql/001_integrity_corporate_actions.sql and the database env vars
(see db.py). stdout carries counts and percentages only; --report writes the
nominal reconciliation (BRL amounts, position sizes) to a local file that must
never be committed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from psycopg2.extensions import cursor as PgCursor

import db
from calculate_daily_twr import MONEY_COLUMNS, RETURN_COLUMNS, SyncPlan, compute, for_storage, plan_sync
from market_data import (
    B3_CALENDAR,
    B3_TZ,
    FETCH_ERRORS,
    NY_TZ,
    NYSE_CALENDAR,
    apply_renames,
    b3_annual_closes,
    b3_codes,
    b3_daily_closes,
    last_completed_session,
    sessions,
    yahoo_raw_history,
    yesterday,
)
from portfolio_engine import (
    PERFORMANCE_COLUMNS,
    EngineResult,
    average_cost_ledger,
    carry_prices,
    income_quantity_reconciliation,
    normalize_corporate_actions,
    normalize_dividends,
    normalize_prices,
    normalize_trades,
    xirr,
)
from prices_pipeline import NO_TRADE_SOURCE, OFFICIAL_SOURCE, PRICE_DECIMALS, classify_action, required_pairs

PRICE_TOLERANCE = 0.005  # half a cent: anything larger is a different close
SQL_CHUNK_ROWS = 2000
CA_ALIGNMENT_TOLERANCE = np.log(1.25)  # large split: implied ratio within ±25% of the recorded one
LARGE_RATIO = 1.5

# Events Yahoo does not carry. Public market data, verified against the issuer.
MANUAL_CORPORATE_ACTIONS = pd.DataFrame(
    [
        {
            "ticker": "CYRE3",
            "ex_date": pd.Timestamp("2026-01-02"),
            "ratio": 1.18,
            "action_type": "BONUS",
            "source": "manual",
            "notes": "0.18 bonus share per share (data-com 2025-12-30), credited 2026-01-06 as CYRE3; "
            "fractions auctioned and paid 2026-02-24.",
        }
    ]
)


def _concat(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """pd.concat that ignores empty frames (no dtype guessing from them)."""
    kept = [f for f in frames if not f.empty]
    return pd.concat(kept, ignore_index=True) if kept else frames[0].iloc[0:0].copy()


# ── Plans ─────────────────────────────────────────────────────────────────────


@dataclass
class PricePlan:
    """Row-level changes to ``prices`` and the resulting table."""

    deletes: pd.DataFrame  # [ticker, date]
    updates: pd.DataFrame  # [ticker, date, price, source]
    inserts: pd.DataFrame  # [ticker, date, price, source]
    final: pd.DataFrame  # [ticker, date, price, source] after the plan
    stats: dict[str, int] = field(default_factory=dict)


@dataclass
class BackfillPlan:
    ledger: dict[str, pd.DataFrame]
    stored_performance: pd.DataFrame
    prices: PricePlan
    benchmark: pd.DataFrame  # full rebuilt table [date, price, source]
    benchmark_stats: dict[str, int]
    corporate_actions: pd.DataFrame  # rows to insert
    ca_checks: pd.DataFrame
    performance: SyncPlan
    result: EngineResult
    new_performance: pd.DataFrame


def official_closes(
    tickers: Sequence[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    b3_sessions: pd.DatetimeIndex,
    cache_dir: Path | None,
) -> pd.DataFrame:
    """Official closes [ticker, date, price] for ``tickers`` over [start, end].

    Annual COTAHIST files cover each year up to the day before B3 refreshed
    them; any later session is read from its daily file.
    """
    codes = b3_codes(tickers)
    parts = [b3_annual_closes(y, codes, cache_dir) for y in range(start.year, end.year + 1)]
    closes = pd.concat(parts, ignore_index=True)
    covered = closes["date"].max()
    for day in b3_sessions[(b3_sessions > covered) & (b3_sessions <= end)]:
        daily = b3_daily_closes(day)
        closes = pd.concat([closes, daily[daily["ticker"].isin(codes)]], ignore_index=True)
    closes = apply_renames(closes)
    closes = closes[closes["ticker"].isin(set(tickers)) & closes["date"].between(start, end)]
    return closes[["ticker", "date", "price"]].sort_values(["ticker", "date"]).reset_index(drop=True)


def plan_prices(
    stored: pd.DataFrame,
    official: pd.DataFrame,
    required: pd.DataFrame,
    b3_sessions: pd.DatetimeIndex,
    corporate_actions: pd.DataFrame,
) -> PricePlan:
    """Align ``prices`` to the official closes.

    Args:
        stored: Current ``prices`` rows.
        official: Output of :func:`official_closes`.
        required: Held (ticker, session) pairs.
        b3_sessions: B3 sessions covering the ledger.
        corporate_actions: Normalized corporate actions (for marker prices).

    Returns:
        PricePlan.
    """
    key = ["ticker", "date"]
    s = normalize_prices(stored)[["ticker", "date", "price", "source"]]
    off = official.rename(columns={"price": "official"})
    held = required.assign(held=True)

    non_session = s[~s["date"].isin(b3_sessions)][key]
    m = (
        s[s["date"].isin(b3_sessions)]
        .merge(off, on=key, how="left")
        .merge(held, on=key, how="left")
        .assign(held=lambda f: f["held"].eq(True))
    )
    has_official = m["official"].notna()

    # (a) official print exists → row must carry it.
    wrong = has_official & (((m["price"] - m["official"]).abs() > PRICE_TOLERANCE) | (m["source"] != OFFICIAL_SOURCE))
    to_official = m.loc[wrong, key].assign(price=m.loc[wrong, "official"], source=OFFICIAL_SOURCE)

    # (b) no print, held → no-trade marker; not held → provider artefact, delete.
    no_print_held = m[~has_official & m["held"]][key]
    artefacts = m[~has_official & ~m["held"]][key]

    # (c) held pairs with no stored row at all.
    absent = held.merge(s[key], on=key, how="left", indicator=True)
    absent = absent.loc[absent["_merge"] == "left_only", key]
    absent = absent.merge(off, on=key, how="left")
    insert_official = absent[absent["official"].notna()].rename(columns={"official": "price"}).assign(source=OFFICIAL_SOURCE)

    marker_pairs = pd.concat([no_print_held, absent.loc[absent["official"].isna(), key]], ignore_index=True)
    marker_prices = carry_prices(marker_pairs, off.rename(columns={"official": "price"}), corporate_actions)
    markers = marker_pairs.assign(price=marker_prices.round(PRICE_DECIMALS), source=NO_TRADE_SOURCE)
    unpriceable = markers[markers["price"].isna()]
    markers = markers.dropna(subset=["price"])

    marker_keys = set(zip(no_print_held["ticker"], no_print_held["date"]))
    is_existing = pd.Series([k in marker_keys for k in zip(markers["ticker"], markers["date"])], dtype=bool).to_numpy()
    existing_markers = markers[is_existing]
    current = s.set_index(key)
    stale_marker = [
        (r.ticker, r.date) not in current.index
        or current.at[(r.ticker, r.date), "source"] != NO_TRADE_SOURCE
        or abs(current.at[(r.ticker, r.date), "price"] - r.price) > PRICE_TOLERANCE
        for r in existing_markers.itertuples()
    ]
    marker_updates = existing_markers[np.array(stale_marker, dtype=bool)] if len(existing_markers) else existing_markers

    updates = pd.concat([to_official, marker_updates], ignore_index=True)
    updates["price"] = updates["price"].astype(float).round(PRICE_DECIMALS)
    inserts = pd.concat([insert_official, markers[~is_existing]], ignore_index=True)
    inserts["price"] = inserts["price"].astype(float).round(PRICE_DECIMALS)
    deletes = pd.concat([non_session, artefacts], ignore_index=True)

    final = (
        s.merge(deletes.assign(_drop=True), on=key, how="left")
        .loc[lambda f: f["_drop"].isna(), ["ticker", "date", "price", "source"]]
        .set_index(key)
    )
    final.update(updates.set_index(key)[["price", "source"]])
    final = pd.concat([final.reset_index(), inserts], ignore_index=True)

    legacy = m[m["source"] != OFFICIAL_SOURCE]
    rel = (legacy["price"] / legacy["official"] - 1).abs()
    stats = {
        "stored_rows": len(s),
        "deleted_non_session": len(non_session),
        "deleted_no_print_not_held": len(artefacts),
        "updated_to_official": len(to_official),
        "updated_value_changed": int(((m["price"] - m["official"]).abs() > PRICE_TOLERANCE)[wrong].sum()),
        "legacy_rows_off_by_more_than_2pct": int((rel > 0.02).sum()),
        "inserted_official": len(insert_official),
        "no_trade_markers_new": int((~is_existing).sum()),
        "no_trade_markers_repriced": len(marker_updates),
        "held_pairs_unpriceable": len(unpriceable),
        "final_rows": len(final),
    }
    return PricePlan(deletes, updates, inserts, final, stats)


def plan_benchmark(
    stored: pd.DataFrame,
    first: pd.Timestamp,
    now: datetime,
    cache_dir: Path | None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Full SPY table from Yahoo raw closes on NYSE sessions, plus a legacy audit.

    Starts at the earlier of the first trade and the oldest stored row, so the
    dashboard keeps the same benchmark window.
    """
    target = last_completed_session(NYSE_CALENDAR, now)
    if not stored.empty:
        first = min(first, pd.Timestamp(pd.to_datetime(stored["date"]).min()))
    nyse = sessions(NYSE_CALENDAR, first, target)
    # Keyed by the target session: a cache from an earlier day never hides newer closes.
    cached = cache_dir / f"yahoo_spy_{target:%Y%m%d}.csv" if cache_dir is not None else None
    if cached is not None and cached.exists():
        spy = pd.read_csv(cached, parse_dates=["date"])
    else:
        spy, _ = yahoo_raw_history("SPY", first - pd.Timedelta(days=7), market="US")
        if cached is not None:
            spy.to_csv(cached, index=False)
    rebuilt = (
        spy[spy["date"].isin(nyse)][["date", "price"]]
        .assign(price=lambda f: f["price"].round(PRICE_DECIMALS), source="yahoo_raw")
        .drop_duplicates("date")
        .reset_index(drop=True)
    )

    old = stored[["date", "price"]].copy()
    old["date"] = pd.to_datetime(old["date"])
    old["price"] = old["price"].astype(float)
    by_date = old.merge(rebuilt, on="date", how="left", suffixes=("", "_new"))
    prev = rebuilt.assign(date=rebuilt["date"].shift(-1)).dropna(subset=["date"])  # price of the session before
    shifted = old.merge(prev, on="date", how="left", suffixes=("", "_prev"))
    exact = (by_date["price"] - by_date["price_new"]).abs() <= PRICE_TOLERANCE
    stats = {
        "nyse_sessions": len(nyse),
        "rebuilt_rows": len(rebuilt),
        "missing_sessions": len(nyse.difference(pd.DatetimeIndex(rebuilt["date"]))),
        "legacy_rows": len(old),
        "legacy_exact": int(exact.sum()),
        "legacy_on_non_session": int(by_date["price_new"].isna().sum()),
        "legacy_equal_to_prior_session": int(
            (~exact.to_numpy() & ((shifted["price"] - shifted["price_prev"]).abs() <= PRICE_TOLERANCE).to_numpy()).sum()
        ),
    }
    return rebuilt, stats


def yahoo_corporate_actions(tickers: Iterable[str], start: pd.Timestamp, cache_dir: Path | None) -> pd.DataFrame:
    """Yahoo split/bonus events [ticker, ex_date, ratio] since ``start``."""
    cached = cache_dir / f"yahoo_actions_{pd.Timestamp.now():%Y%m%d}.csv" if cache_dir is not None else None
    if cached is not None and cached.exists():
        events = pd.read_csv(cached, parse_dates=["ex_date"])
        return events[events["ex_date"] >= start]
    rows: list[pd.DataFrame] = []
    for tk in sorted(tickers):
        try:
            _, actions = yahoo_raw_history(tk, start)
        except FETCH_ERRORS as exc:
            raise RuntimeError(f"Yahoo corporate actions unavailable for {tk} ({type(exc).__name__})") from None
        rows.append(actions.assign(ticker=tk))
    events = pd.concat(rows, ignore_index=True)[["ticker", "ex_date", "ratio"]] if rows else pd.DataFrame()
    if cached is not None:
        events.to_csv(cached, index=False)
    return events


def plan_corporate_actions(stored: pd.DataFrame, yahoo: pd.DataFrame) -> pd.DataFrame:
    """Events to insert: Yahoo + manual, minus what is already recorded."""
    events = yahoo.assign(
        action_type=yahoo["ratio"].map(classify_action), source="yahoo", notes=None
    )
    events = pd.concat([events, MANUAL_CORPORATE_ACTIONS], ignore_index=True)
    events["ex_date"] = pd.to_datetime(events["ex_date"])
    known = set(zip(stored["ticker"], pd.to_datetime(stored["ex_date"]))) if not stored.empty else set()
    fresh = np.array([(tk, d) not in known for tk, d in zip(events["ticker"], events["ex_date"])], dtype=bool)
    return events[fresh].drop_duplicates(["ticker", "ex_date"]).reset_index(drop=True)


def check_corporate_actions(actions: pd.DataFrame, official: pd.DataFrame) -> pd.DataFrame:
    """Do official closes jump by the recorded ratio on the ex-date?

    Only large events are testable (a 5% bonus hides in normal volatility).
    """
    out: list[dict] = []
    official = official.sort_values(["ticker", "date"])
    for ev in normalize_corporate_actions(actions).itertuples(index=False):
        tk = official[official["ticker"] == ev.ticker]
        before = tk[tk["date"] < ev.ex_date].tail(1)
        after = tk[tk["date"] >= ev.ex_date].head(1)
        implied = float(before["price"].iloc[0] / after["price"].iloc[0]) if len(before) and len(after) else np.nan
        if abs(np.log(ev.ratio)) < np.log(LARGE_RATIO):
            status = "SMALL_UNTESTABLE"
        elif np.isnan(implied):
            status = "NO_PRINTS"
        else:
            status = "ALIGNED" if abs(np.log(implied / ev.ratio)) < CA_ALIGNMENT_TOLERANCE else "MISALIGNED"
        out.append({"ticker": ev.ticker, "ex_date": ev.ex_date, "ratio": ev.ratio, "implied": implied, "status": status})
    return pd.DataFrame(out)


def require_migration(conn: db.PgConnection) -> None:
    """Fail fast with a clear message if sql/001 has not been applied."""
    check = db.read_frame(
        conn,
        "SELECT COUNT(*) AS n FROM information_schema.columns "
        "WHERE table_schema = 'public' AND ((table_name = 'prices' AND column_name = 'source') "
        "OR (table_name = 'corporate_actions' AND column_name = 'ratio') "
        "OR (table_name = 'portfolio_performance' AND column_name = 'is_trading_day'))",
    )
    if int(check["n"].iloc[0]) != 3:
        raise SystemExit("schema is missing sql/001_integrity_corporate_actions.sql — apply it first")


def build_plan(conn: db.PgConnection, end: pd.Timestamp, now: datetime, cache_dir: Path | None) -> BackfillPlan:
    """Read everything, plan every table, recompute performance."""
    require_migration(conn)
    ledger = db.load_ledger(conn)
    stored_benchmark = db.read_frame(conn, "SELECT date, price FROM benchmark")
    stored_perf = db.read_frame(conn, f"SELECT {', '.join(PERFORMANCE_COLUMNS)} FROM portfolio_performance")

    trades = normalize_trades(ledger["trades"])
    stored_prices = normalize_prices(ledger["prices"])
    first = trades["date"].min()
    # Stored closes predate the first trade (the 2026-03 bulk load starts in
    # January 2024): align those too, so every row in the table is official.
    price_start = min(first, stored_prices["date"].min()) if not stored_prices.empty else first
    tickers = sorted(set(trades["ticker"]) | set(stored_prices["ticker"]))
    price_end = last_completed_session(B3_CALENDAR, now)
    b3 = sessions(B3_CALENDAR, first, price_end)
    b3_all = sessions(B3_CALENDAR, price_start, price_end)

    yahoo_ca = yahoo_corporate_actions(sorted(trades["ticker"].unique()), first - pd.Timedelta(days=1), cache_dir)
    new_ca = plan_corporate_actions(ledger["corporate_actions"], yahoo_ca)
    all_ca_raw = _concat([ledger["corporate_actions"], new_ca])
    all_ca = normalize_corporate_actions(all_ca_raw)

    required = required_pairs({"trades": ledger["trades"], "corporate_actions": all_ca_raw}, b3)
    official = official_closes(tickers, price_start, price_end, b3_all, cache_dir)
    price_plan = plan_prices(ledger["prices"], official, required, b3_all, all_ca)
    benchmark, bench_stats = plan_benchmark(stored_benchmark, first, now, cache_dir)

    rebuilt = dict(ledger, prices=price_plan.final, corporate_actions=all_ca_raw)
    result = compute(rebuilt, end)
    new_perf = for_storage(result.performance)
    perf_plan = plan_sync(new_perf, stored_perf) if result.ok else SyncPlan(new_perf.iloc[0:0], [], 0, None, None)
    return BackfillPlan(
        ledger=ledger,
        stored_performance=stored_perf,
        prices=price_plan,
        benchmark=benchmark,
        benchmark_stats=bench_stats,
        corporate_actions=new_ca,
        ca_checks=check_corporate_actions(all_ca_raw, official),
        performance=perf_plan,
        result=result,
        new_performance=new_perf,
    )


# ── SQL rendering (one code path for --apply and --emit-sql) ─────────────────


def _sql_value(value: object) -> object:
    """Python value for mogrify; every timestamp in these tables is a DATE."""
    if isinstance(value, pd.Timestamp):
        return value.date()
    return db._py(value)


def _rows(cur: PgCursor, frame: pd.DataFrame, columns: Sequence[str]) -> list[str]:
    template = "(" + ", ".join(["%s"] * len(columns)) + ")"
    return [
        cur.mogrify(template, tuple(_sql_value(v) for v in row)).decode()
        for row in frame[list(columns)].itertuples(index=False, name=None)
    ]


def _chunks(values: list[str]) -> Iterable[str]:
    for start in range(0, len(values), SQL_CHUNK_ROWS):
        yield ",\n".join(values[start : start + SQL_CHUNK_ROWS])


def plan_statements(cur: PgCursor, plan: BackfillPlan, stamp: str, today: datetime) -> list[str]:
    """Every statement of the repair, in execution order (no BEGIN/COMMIT)."""
    out = [
        "CREATE SCHEMA IF NOT EXISTS backup",
        *(
            f"CREATE TABLE backup.{t}_pre_backfill_{stamp} AS TABLE public.{t}"
            for t in ("prices", "benchmark", "corporate_actions", "portfolio_performance")
        ),
    ]
    p = plan.prices
    if not p.deletes.empty:
        for chunk in _chunks(_rows(cur, p.deletes, ["ticker", "date"])):
            out.append(f"DELETE FROM public.prices WHERE (ticker, date) IN (VALUES\n{chunk})")
    if not p.updates.empty:
        for chunk in _chunks(_rows(cur, p.updates, ["ticker", "date", "price", "source"])):
            out.append(
                "UPDATE public.prices AS p SET price = v.price, source = v.source\n"
                f"FROM (VALUES\n{chunk}) AS v(ticker, date, price, source)\n"
                "WHERE p.ticker = v.ticker AND p.date = v.date"
            )
    if not p.inserts.empty:
        rows = p.inserts.assign(created_at=today.date())
        for chunk in _chunks(_rows(cur, rows, ["ticker", "date", "price", "source", "created_at"])):
            out.append(
                "INSERT INTO public.prices (ticker, date, price, source, created_at) VALUES\n"
                f"{chunk}\nON CONFLICT (ticker, date) DO NOTHING"
            )

    out.append("DELETE FROM public.benchmark")
    bench = plan.benchmark.assign(created_at=today.date())
    for chunk in _chunks(_rows(cur, bench, ["date", "price", "source", "created_at"])):
        out.append(f"INSERT INTO public.benchmark (date, price, source, created_at) VALUES\n{chunk}")

    if not plan.corporate_actions.empty:
        cols = ["ticker", "ex_date", "ratio", "action_type", "source", "notes"]
        for chunk in _chunks(_rows(cur, plan.corporate_actions, cols)):
            out.append(
                f"INSERT INTO public.corporate_actions ({', '.join(cols)}) VALUES\n{chunk}\n"
                "ON CONFLICT (ticker, ex_date) DO NOTHING"
            )

    perf = plan.performance
    if not perf.upserts.empty:
        cols = PERFORMANCE_COLUMNS
        assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "date")
        for chunk in _chunks(_rows(cur, perf.upserts, cols)):
            out.append(
                f"INSERT INTO public.portfolio_performance ({', '.join(cols)}) VALUES\n{chunk}\n"
                f"ON CONFLICT (date) DO UPDATE SET {assignments}, updated_at = now()"
            )
    if perf.deletes:
        dates = ", ".join(cur.mogrify("%s", (d.date(),)).decode() for d in perf.deletes)
        out.append(f"DELETE FROM public.portfolio_performance WHERE date IN ({dates})")
    return out


# ── Reporting ─────────────────────────────────────────────────────────────────


def performance_stats(perf: pd.DataFrame) -> dict[str, float]:
    """Investor-style statistics on B3 sessions (percentages only)."""
    s = perf[perf["is_trading_day"]].set_index("date")
    nav = 1.0 + perf.set_index("date")["twr"]
    years = (perf["date"].iloc[-1] - perf["date"].iloc[0]).days / 365.25
    daily = s["daily_return"]
    drawdown = nav / nav.cummax() - 1.0
    return {
        "twr_total": float(perf["twr"].iloc[-1]),
        "twr_annualised": float(nav.iloc[-1] ** (1 / years) - 1) if years > 0 else np.nan,
        "volatility_annualised": float(daily.std(ddof=1) * np.sqrt(252)),
        "max_drawdown": float(drawdown.min()),
        "sessions": int(len(s)),
    }


def calendar_year_returns(perf: pd.DataFrame) -> pd.Series:
    """Chain-linked TWR per calendar year (current year = YTD)."""
    nav = (1.0 + perf.set_index("date")["twr"])
    year_end = nav.groupby(nav.index.year).last()
    start = pd.concat([pd.Series([1.0], index=[year_end.index[0]]), year_end.shift(1).dropna()])
    start.index = year_end.index
    return year_end / start - 1.0


def money_weighted_return(perf: pd.DataFrame) -> float:
    """XIRR of contributions vs. the final portfolio value."""
    flows = perf.loc[perf["external_inflow"] > 0, ["date", "external_inflow"]]
    dates = pd.concat([flows["date"], perf["date"].tail(1)], ignore_index=True)
    amounts = pd.concat([-flows["external_inflow"], perf["portfolio_value"].tail(1)], ignore_index=True)
    return xirr(dates, amounts)


def pnl_attribution(ledger: dict[str, pd.DataFrame], plan: BackfillPlan, ca: pd.DataFrame) -> dict[str, float]:
    """Average-cost P&L split and the accounting identity check (BRL)."""
    end = plan.new_performance["date"].iloc[-1]
    trades = ledger["trades"][pd.to_datetime(ledger["trades"]["date"]) <= end]
    events = average_cost_ledger(trades, ca[ca["ex_date"] <= end])
    last = events.groupby("ticker").tail(1).set_index("ticker")
    held = last[last["quantity_after"] > 0]
    closes = plan.result.prices.loc[end, held.index]
    unrealised = float((held["quantity_after"] * closes - held["cost_basis_after"]).sum())
    realised = float(events["realised_pnl"].sum())
    divs = normalize_dividends(ledger["dividends"])
    first = pd.to_datetime(ledger["trades"]["date"]).min()
    income = float(divs.loc[divs["date"].between(first, end), "total"].sum())
    row = plan.new_performance.iloc[-1]
    gain = float(row["portfolio_value"] - row["total_invested"])
    return {
        "portfolio_value": float(row["portfolio_value"]),
        "total_invested": float(row["total_invested"]),
        "gain": gain,
        "realised": realised,
        "unrealised": unrealised,
        "income": income,
        "identity_gap": gain - (realised + unrealised + income),
        "equity_engine": float(row["equity_value"]),
        "equity_avg_cost_ledger": float((held["quantity_after"] * closes).sum()),
    }


def irpf_asset_class(asset: pd.Series, ticker: pd.Series) -> pd.Series:
    """IRPF class per trade from the nota's specification column.

    ACOES = shares and units (the only class eligible for the R$20k/month
    swing-trade exemption); BDR and ETF gains are taxed at 15%, FII at 20%.
    Sinacor specs: "APPLE DRN", "ISHARES BOVA CI", "FII XP LOG CI".
    """
    spec = asset.astype(str).str.upper()
    is_fii = spec.str.contains(r"\bFII\b")
    is_etf = spec.str.contains(r"\bCI\b") & ~is_fii
    is_bdr = spec.str.contains(r"\bDRN\b|\bDR[1-3]\b") | ticker.str.match(r"^[A-Z0-9]{4}3[2-9]$")
    return pd.Series(np.select([is_fii, is_etf, is_bdr], ["FII", "ETF", "BDR"], default="ACOES"), index=asset.index)


def monthly_tax_view(ledger: dict[str, pd.DataFrame], ca: pd.DataFrame) -> pd.DataFrame:
    """Monthly sales and realised P&L by IRPF class (indicative — confirm with DARF/accountant).

    Swing-trade sales of ACOES up to R$20k/month are exempt; BDR, ETF and FII
    gains are always taxable. A month with day trades is never auto-flagged
    exempt. Custo atribuído of bonus shares is NOT in the basis (add it per
    issuer notice).
    """
    t = normalize_trades(ledger["trades"])
    asset_class = irpf_asset_class(t["asset"], t["ticker"])
    events = average_cost_ledger(ledger["trades"], ca)
    sells = events[events["event"] == "SELL"].reset_index(drop=True)
    sells_raw = t[t["side"] == "SELL"].reset_index(drop=True)
    day_trade_keys = set(t[t["side"] == "BUY"][["ticker", "date"]].itertuples(index=False, name=None))
    frame = pd.DataFrame(
        {
            "month": sells_raw["date"].dt.to_period("M").astype(str),
            "asset_class": asset_class[t["side"] == "SELL"].to_numpy(),
            "gross_sales": sells_raw["total"].to_numpy(),
            "realised_pnl": sells["realised_pnl"].to_numpy(),
            "day_trade": [(r.ticker, r.date) in day_trade_keys for r in sells_raw.itertuples()],
        }
    )
    out = frame.groupby(["month", "asset_class"]).agg(
        gross_sales=("gross_sales", "sum"), realised_pnl=("realised_pnl", "sum"), day_trades=("day_trade", "sum")
    ).reset_index()
    out["exempt_swing"] = (out["asset_class"] == "ACOES") & (out["gross_sales"] <= 20000.0) & (out["day_trades"] == 0)
    return out


def print_summary(plan: BackfillPlan, ledger: dict[str, pd.DataFrame], stored_perf_twr: float | None) -> None:
    """Public-safe console report: counts and percentages only."""
    print("\n══ prices ══")
    for k, v in plan.prices.stats.items():
        print(f"  {k:36s} {v:>8,}")
    print("══ benchmark (SPY) ══")
    for k, v in plan.benchmark_stats.items():
        print(f"  {k:36s} {v:>8,}")
    print("══ corporate actions ══")
    print(f"  new rows to insert: {len(plan.corporate_actions)}")
    for r in plan.ca_checks.itertuples():
        implied = "" if np.isnan(r.implied) else f" implied {r.implied:.3f}"
        print(f"  {r.ticker:7s} ex {r.ex_date.date()} ×{r.ratio:<9g}{implied:16s} {r.status}")
    print("══ engine ══")
    counts = pd.Series([i.code for i in plan.result.issues], dtype=str).value_counts()
    print(f"  ok={plan.result.ok}  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    for i in plan.result.issues:
        print(f"  [{i.severity}] {i.code}: {i.message}")
    if not plan.result.ok:
        return
    perf = plan.new_performance
    stats = performance_stats(perf)
    print("══ performance (B3 sessions) ══")
    print(f"  series {perf['date'].iloc[0].date()} → {perf['date'].iloc[-1].date()} ({stats['sessions']} sessions)")
    if stored_perf_twr is not None:
        print(f"  TWR stored before repair     {stored_perf_twr:+.2%}")
    print(f"  TWR total                    {stats['twr_total']:+.2%}")
    print(f"  TWR annualised               {stats['twr_annualised']:+.2%}")
    print(f"  money-weighted (XIRR)        {money_weighted_return(perf):+.2%}")
    print(f"  volatility annualised        {stats['volatility_annualised']:.2%}")
    print(f"  max drawdown                 {stats['max_drawdown']:+.2%}")
    for year, r in calendar_year_returns(perf).items():
        print(f"  calendar {year}                {r:+.2%}")
    ca = normalize_corporate_actions(_concat([ledger["corporate_actions"], plan.corporate_actions]))
    attribution = pnl_attribution(ledger, plan, ca)
    ok = abs(attribution["identity_gap"]) < 0.01 and abs(attribution["equity_engine"] - attribution["equity_avg_cost_ledger"]) < 0.01
    print(f"  accounting identity (gain = realised + unrealised + income): {'OK' if ok else 'FAILED'}")
    recon = income_quantity_reconciliation(plan.result.positions, ledger["dividends"])
    print(f"  income rows vs positions: {recon['status'].value_counts().to_dict()}")
    print(f"  performance rows: upsert {len(plan.performance.upserts)}, delete {len(plan.performance.deletes)}")


def write_report(path: Path, plan: BackfillPlan, ledger: dict[str, pd.DataFrame], stored_perf: pd.DataFrame) -> None:
    """Nominal reconciliation for the owner (local file; never commit)."""
    perf = plan.new_performance
    ca = normalize_corporate_actions(_concat([ledger["corporate_actions"], plan.corporate_actions]))
    a = pnl_attribution(ledger, plan, ca)
    brl = lambda v: f"R$ {v:,.2f}"  # noqa: E731
    lines = [
        f"# Portfolio reconciliation — {perf['date'].iloc[-1].date()}",
        "",
        "> Local file with nominal values. Do not commit or share.",
        "",
        "## Position as of the last day",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Portfolio value | {brl(a['portfolio_value'])} |",
        f"| Total invested (net contributions) | {brl(a['total_invested'])} |",
        f"| Gain | {brl(a['gain'])} |",
        f"| ├ realised (average cost, after fees) | {brl(a['realised'])} |",
        f"| ├ unrealised | {brl(a['unrealised'])} |",
        f"| └ income (dividends, JCP, rendimentos, frações) | {brl(a['income'])} |",
        f"| Identity gap (must be ~0) | {brl(a['identity_gap'])} |",
        f"| TWR | {perf['twr'].iloc[-1]:+.2%} |",
        f"| XIRR | {money_weighted_return(perf):+.2%} |",
        "",
        "## Month-end: rebuilt vs stored before repair",
        "",
        "| Month | Value | Invested | TWR | TWR (old) |",
        "|---|---|---|---|---|",
    ]
    old = stored_perf.assign(date=pd.to_datetime(stored_perf["date"])).set_index("date")["twr"].astype(float)
    month_end = perf.set_index("date").resample("ME").last()
    for d, r in month_end.iterrows():
        o = old.reindex([d]).iloc[0] if d in old.index else np.nan
        lines.append(f"| {d:%Y-%m} | {brl(r['portfolio_value'])} | {brl(r['total_invested'])} | {r['twr']:+.2%} | "
                     f"{'' if np.isnan(o) else f'{o:+.2%}'} |")

    lines += ["", "## Corporate actions applied to holdings", "", "| Ticker | Ex-date | Ratio | Before | After | Fraction paid in cash |", "|---|---|---|---|---|---|"]
    for r in plan.result.corporate_action_log.itertuples():
        lines.append(f"| {r.ticker} | {r.ex_date.date()} | {r.ratio:g} | {r.shares_before:g} | {r.shares_after:g} | {r.fraction_paid_in_cash:g} |")

    recon = income_quantity_reconciliation(plan.result.positions, ledger["dividends"])
    bad = recon[recon["status"] != "OK"]
    lines += ["", "## Income rows whose share count matches no position (verify vs broker)", "", "| Ticker | Paid | Type | Qty on row | Status |", "|---|---|---|---|---|"]
    for r in bad.itertuples():
        lines.append(f"| {r.ticker} | {r.paid.date()} | {r.type} | {r.quantity} | {r.status} |")

    t = normalize_trades(ledger["trades"])
    neg = t[t["fee"] < 0]
    lines += ["", "## Trades with negative fees (verify vs nota de corretagem)", "", "| Date | Ticker | Side | Total | Fee |", "|---|---|---|---|---|"]
    for r in neg.itertuples():
        lines.append(f"| {r.date.date()} | {r.ticker} | {r.side} | {brl(r.total)} | {brl(r.fee)} |")

    tax = monthly_tax_view(ledger, ca)
    lines += [
        "",
        "## IRPF view — monthly sales and realised P&L",
        "",
        "Shares (ações, units) swing trades are exempt up to R$20k gross sales/month; BDR gains are always taxable "
        "(15%). Day trades are taxed separately (20%). Units are grouped with shares — confirm with your "
        "accountant. Bonus-share custo atribuído and auctioned fractions (booked as income) are not in the cost "
        "basis.",
        "",
        "| Month | Class | Gross sales | Realised P&L | Day trades | Exempt (swing ≤ R$20k) |",
        "|---|---|---|---|---|---|",
    ]
    for r in tax.itertuples():
        lines.append(f"| {r.month} | {r.asset_class} | {brl(r.gross_sales)} | {brl(r.realised_pnl)} | {r.day_trades} | {'yes' if r.exempt_swing else 'no'} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="execute the plan in one transaction")
    mode.add_argument("--emit-sql", type=Path, help="write the plan's SQL to this file instead")
    parser.add_argument("--cache-dir", type=Path, help="reuse/stash B3 annual files and Yahoo data here")
    parser.add_argument("--end", type=pd.Timestamp, help="last performance day (default: yesterday, São Paulo)")
    parser.add_argument("--report", type=Path, help="write the nominal reconciliation report here (local only)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Plan, report and optionally apply the repair. Returns the exit code."""
    args = parse_args(argv)
    load_dotenv()
    now = datetime.now(B3_TZ)
    end = pd.Timestamp(args.end) if args.end is not None else yesterday(B3_TZ, now)
    if args.cache_dir is not None:
        args.cache_dir.mkdir(parents=True, exist_ok=True)

    # Plan and apply share ONE transaction: the writes are computed from exactly
    # the rows they replace, and any failure rolls everything back.
    with db.connect() as conn:
        plan = build_plan(conn, end, now, args.cache_dir)
        stored_perf = plan.stored_performance
        old_twr = None
        if not stored_perf.empty:
            match = stored_perf[pd.to_datetime(stored_perf["date"]) == plan.new_performance["date"].iloc[-1]]
            old_twr = float(match["twr"].iloc[0]) if not match.empty else None
        print_summary(plan, plan.ledger, old_twr)
        if args.report is not None and plan.result.ok:
            write_report(args.report, plan, plan.ledger, stored_perf)
            print(f"\nnominal report written to {args.report} (local only — do not commit)")
        if not plan.result.ok:
            print("\nengine reported ERRORS — refusing to write anything", file=sys.stderr)
            return 1

        with conn.cursor() as cur:
            statements = plan_statements(cur, plan, now.strftime("%Y%m%d%H%M"), now)
            if args.apply:
                for statement in statements:
                    cur.execute(statement)
    if args.emit_sql is not None:
        args.emit_sql.write_text("BEGIN;\n\n" + ";\n\n".join(statements) + ";\n\nCOMMIT;\n", encoding="utf-8")
        print(f"\n{len(statements)} statements written to {args.emit_sql}")
    elif args.apply:
        print(f"\napplied {len(statements)} statements in one transaction")
    else:
        print(f"\ndry run — {len(statements)} statements planned; re-run with --apply or --emit-sql PATH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
