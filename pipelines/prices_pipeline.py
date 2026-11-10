"""
prices_pipeline.py
──────────────────
Self-healing sync of OFFICIAL B3 closes for every session the portfolio needs.

Each run derives — from trades + corporate actions — every (ticker, session)
on which a position is held, diffs that against ``prices`` and fetches only
the gaps. Normally that is just the last session, but a missed run, a ticker
bought before its trade was entered, or a ledger correction is backfilled
automatically on the next run.

Layers
  1. B3 COTAHIST daily file (official, explicitly dated; B3 keeps them for
     years), newest sessions first, at most B3_MAX_FILES_PER_RUN downloads.
     A held ticker absent from a session's file did not trade: a
     'b3_no_trade' marker is stored (price = last close, corporate-action
     adjusted) and the engine carries the last print.
  2. Yahoo raw, then BRAPI — only when B3 is unavailable or over budget.
     A later run replaces their rows with the official close, or with a
     no-trade marker when the B3 file shows no standard-lot trade.

Rules
  • Only bars dated on B3 sessions are stored; zero-volume provider bars are
    carried quotes and are never stored as closes.
  • Official rows are never overwritten (ON CONFLICT DO NOTHING); a stored
    close disagreeing with a provider by >2% is reported, not applied.
  • No placeholder prices. A held ticker with neither a print nor a confirmed
    no-trade on the latest session alerts on Telegram and exits 1.
  • Network time is bounded (B3: failure cutoff + time budget; then a budget
    for the whole fetch phase), so the run always reaches the write and
    stores whatever it fetched.
  • Telegram also flags a latest session still on a fallback source (B3 file
    missing) and any corporate-action check that could not run.

Also records new splits / bonus issues for recently held tickers from Yahoo
into ``corporate_actions`` (the engine applies them to share counts). A Yahoo
event within CA_MATCH_DAYS of a stored row for the same ticker counts as
recorded, so a hand-corrected date or ratio, or ratio = 1 to reject an
event, is never duplicated or brought back.

Requires sql/001_integrity_corporate_actions.sql (UNIQUE(ticker, date),
``prices.source``, ``corporate_actions``).
"""

from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

import db
from alerts import escape, send_telegram
from market_data import (
    B3_CALENDAR,
    B3_FILE_DEADLINE_S,
    B3_TZ,
    FETCH_ERRORS,
    apply_renames,
    b3_daily_closes,
    brapi_history,
    last_completed_session,
    sessions,
    yahoo_raw_history,
)
from portfolio_engine import (
    build_positions,
    carry_prices,
    normalize_corporate_actions,
    normalize_prices,
    normalize_trades,
    printed_prices,
)

B3_MAX_FILES_PER_RUN = 60  # ~0.5 MB each; the rest falls back and is upgraded next run
B3_MAX_CONSECUTIVE_FAILURES = 3  # B3 down or hanging: stop retrying, fall back
B3_TIME_BUDGET_S = 600.0  # all B3 downloads in one run
FETCH_BUDGET_S = 1200.0  # whole fetch phase; the 30-min job also runs setup and the SPY step
BRAPI_MAX_GAP_SESSIONS = 18  # BRAPI range=1mo returns ~21 sessions
DISAGREEMENT_THRESHOLD = 0.02
CA_LOOKBACK_DAYS = 100
# Yahoo dates a B3 event on the data-com, ex or share-credit day, a few
# sessions apart even across Carnival; no issuer runs two share-count events
# on one ticker within 10 days, so a match this close is the same event.
CA_MATCH_DAYS = 10
REQUEST_PAUSE_S = 0.4
PRICE_DECIMALS = 2  # prices.price is NUMERIC(12,2); B3 tick is R$0.01
OFFICIAL_SOURCE = "b3_cotahist"
NO_TRADE_SOURCE = "b3_no_trade"
FALLBACK_SOURCES = ("yahoo_raw", "brapi")
PRICE_COLUMNS = ["ticker", "date", "price", "source"]

YahooCache = dict[str, tuple[pd.Timestamp, pd.DataFrame, pd.DataFrame]]  # ticker → (start, prices, actions)


def _empty_prices() -> pd.DataFrame:
    return pd.DataFrame(
        {"ticker": pd.Series(dtype=str), "date": pd.Series(dtype="datetime64[ns]"),
         "price": pd.Series(dtype=float), "source": pd.Series(dtype=str)}
    )


def _pairs(rows: list[tuple[str, pd.Timestamp]]) -> pd.DataFrame:
    """Typed [ticker, date] frame (typed even when empty, so merges never fail)."""
    return pd.DataFrame(
        {
            "ticker": pd.Series([r[0] for r in rows], dtype=str),
            "date": pd.Series([r[1] for r in rows], dtype="datetime64[ns]"),
        }
    )


def anti_join(left: pd.DataFrame, right: pd.DataFrame, key: list[str]) -> pd.DataFrame:
    """Rows of ``left`` whose ``key`` is absent from ``right`` (safe when empty)."""
    if left.empty or right.empty:
        return left.copy()
    merged = left.merge(right[key].drop_duplicates(), on=key, how="left", indicator=True)
    return merged.loc[merged["_merge"] == "left_only", list(left.columns)].reset_index(drop=True)


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else _empty_prices()


def required_pairs(ledger: dict[str, pd.DataFrame], b3_sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Every (ticker, session) with a position held at that session's close."""
    trades = normalize_trades(ledger["trades"])
    actions = normalize_corporate_actions(ledger["corporate_actions"])
    days = pd.date_range(trades["date"].min(), b3_sessions[-1], freq="D")
    positions, _ = build_positions(trades, actions, days)
    held = positions.reindex(b3_sessions[b3_sessions >= days[0]]) > 0
    long = held.rename_axis("date").reset_index().melt(id_vars="date", var_name="ticker", value_name="held")
    return long.loc[long["held"], ["ticker", "date"]].reset_index(drop=True)


def missing_pairs(required: pd.DataFrame, stored: pd.DataFrame) -> pd.DataFrame:
    """Required (ticker, date) pairs with no stored row (print or marker)."""
    have = normalize_prices(stored)[["ticker", "date"]] if not stored.empty else stored
    merged = required.merge(have, on=["ticker", "date"], how="left", indicator=True)
    return merged.loc[merged["_merge"] == "left_only", ["ticker", "date"]].reset_index(drop=True)


def upgradable_pairs(required: pd.DataFrame, stored: pd.DataFrame) -> pd.DataFrame:
    """Held pairs still holding a fallback-provider close (not yet official)."""
    if stored.empty:
        return required.iloc[0:0]
    s = normalize_prices(stored)
    fallback = s[s["source"].isin(FALLBACK_SOURCES)][["ticker", "date"]]
    return required.merge(fallback, on=["ticker", "date"])


def fetch_official(
    pairs: pd.DataFrame,
    max_files: int = B3_MAX_FILES_PER_RUN,
    time_budget_s: float = B3_TIME_BUDGET_S,
    max_failures: int = B3_MAX_CONSECUTIVE_FAILURES,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    """Official closes for ``pairs``, one B3 daily file per session.

    Newest sessions first, so the current valuation is settled before any
    deep history when the download budget runs out. Downloads stop after
    ``max_failures`` consecutive failures or ``time_budget_s`` seconds, so a
    hanging host cannot run the job into its timeout; sessions not attempted
    fall back to the other providers like over-budget ones.

    Args:
        pairs: [ticker, date] to settle.
        max_files: Most session files downloaded per run.
        time_budget_s: Seconds for all downloads; each file also gets at most
            what is left of it.
        max_failures: Consecutive unavailable files after which B3 counts as down.
        clock: Monotonic seconds (injectable for tests).

    Returns:
        (prints, no_trade, observed, notes) — ``prints`` [ticker, date, price,
        source]; ``no_trade`` [ticker, date] confirmed absent from a valid
        session file; ``observed`` every held-ticker print in the files read.
    """
    days = sorted(pairs["date"].unique(), reverse=True)
    budget = set(days[:max_files])
    todo = pairs[pairs["date"].isin(budget)]
    notes_over = [f"B3 download budget reached — {len(days) - max_files} session(s) left for later runs"] if len(days) > max_files else []
    prints: list[pd.DataFrame] = []
    observed: list[pd.DataFrame] = []
    no_trade: list[tuple[str, pd.Timestamp]] = []
    notes: list[str] = list(notes_over)
    groups = list(todo.sort_values("date", ascending=False).groupby("date", sort=False))
    start, failures, attempted = clock(), 0, 0
    for day, grp in groups:
        elapsed = clock() - start
        if failures >= max_failures or elapsed >= time_budget_s:
            reason = f"{failures} consecutive failures" if failures >= max_failures else f"{time_budget_s:.0f}s"
            notes.append(f"B3 downloads stopped after {reason} — {len(groups) - attempted} session(s) left to the fallbacks")
            break
        attempted += 1
        try:
            closes = apply_renames(b3_daily_closes(day, deadline_s=min(B3_FILE_DEADLINE_S, time_budget_s - elapsed)))
        except FETCH_ERRORS as exc:
            failures += 1
            notes.append(f"B3 file for {pd.Timestamp(day).date()} unavailable ({type(exc).__name__})")
            continue
        finally:
            time.sleep(REQUEST_PAUSE_S)
        failures = 0
        hit = closes[closes["ticker"].isin(set(grp["ticker"]))]
        prints.append(hit[["ticker", "date", "price"]].assign(source=OFFICIAL_SOURCE))
        observed.append(hit[["ticker", "date", "price"]])
        no_trade += [(tk, pd.Timestamp(day)) for tk in sorted(set(grp["ticker"]) - set(hit["ticker"]))]
    return _concat(prints), _pairs(no_trade), _concat(observed), notes


def yahoo_cached(ticker: str, start: pd.Timestamp, cache: YahooCache) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Yahoo raw history from ``start``, reusing a cached fetch that covers it."""
    hit = cache.get(ticker)
    if hit is None or hit[0] > start:
        prices, actions = yahoo_raw_history(ticker, start)
        cache[ticker] = (start, prices, actions)
        time.sleep(REQUEST_PAUSE_S)
    _, prices, actions = cache[ticker]
    return prices, actions


def fetch_fallback(
    ticker: str,
    dates: pd.DatetimeIndex,
    b3_sessions: pd.DatetimeIndex,
    brapi_token: str | None,
    yahoo_cache: YahooCache,
    history_start: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Gaps B3 could not fill: Yahoo raw first, then BRAPI (recent only).

    Args:
        ticker: B3 ticker.
        dates: Sessions still unfilled.
        b3_sessions: All B3 sessions through the target.
        brapi_token: BRAPI token, or None to skip that layer.
        yahoo_cache: Shared per-run Yahoo cache.
        history_start: Earliest date the run needs from Yahoo for this ticker
            (also covers the corporate-action lookback → one download).

    Returns:
        (rows, observed, notes) as in :func:`fetch_official`.
    """
    wanted = set(dates)
    found: list[pd.DataFrame] = []
    observed: list[pd.DataFrame] = []
    notes: list[str] = []

    try:
        bars, _ = yahoo_cached(ticker, min(history_start, min(wanted) - pd.Timedelta(days=7)), yahoo_cache)
        observed.append(bars[["date", "price"]])
        hit = bars[bars["date"].isin(wanted)][["date", "price"]]
        found.append(hit.assign(source="yahoo_raw"))
        wanted -= set(hit["date"])
    except FETCH_ERRORS as exc:
        notes.append(f"{ticker}: Yahoo failed ({type(exc).__name__}) — trying BRAPI")

    if wanted and brapi_token and max(wanted) >= b3_sessions[-BRAPI_MAX_GAP_SESSIONS:][0]:
        try:
            bars = brapi_history(ticker, brapi_token, "1mo")
            observed.append(bars)
            hit = bars[bars["date"].isin(wanted)]
            found.append(hit.assign(source="brapi"))
            wanted -= set(hit["date"])
        except FETCH_ERRORS as exc:
            notes.append(f"{ticker}: BRAPI failed ({type(exc).__name__})")
        time.sleep(REQUEST_PAUSE_S)

    def _frame(parts: list[pd.DataFrame], cols: list[str]) -> pd.DataFrame:
        out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
        return out[out["date"].isin(b3_sessions)].assign(ticker=ticker)

    rows = _frame(found, ["date", "price", "source"])
    seen = _frame(observed, ["date", "price"]).drop_duplicates(["ticker", "date"])
    if wanted:
        notes.append(f"{ticker}: {len(wanted)} session(s) unfilled, latest {max(wanted).date()}")
    return rows[PRICE_COLUMNS] if not rows.empty else _empty_prices(), seen, notes


def classify_action(ratio: float) -> str:
    """corporate_actions.action_type for a share-count ratio."""
    if ratio < 1:
        return "REVERSE_SPLIT"
    return "SPLIT" if float(ratio).is_integer() else "BONUS"


def sync_corporate_actions(
    tickers: list[str],
    stored: pd.DataFrame,
    yahoo_cache: YahooCache,
    since: pd.Timestamp,
    deadline: float = math.inf,
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Yahoo split/bonus events since ``since`` for ``tickers`` not yet recorded.

    An event counts as recorded when the ticker already has a row — stored or
    added earlier in this run — within CA_MATCH_DAYS of its ex-date, whatever
    the ratio: a corrected date or ratio, or a rejection (ratio = 1), stays
    as the operator left it.

    Args:
        tickers: Tickers held since ``since``.
        stored: ``corporate_actions`` rows already in the database.
        yahoo_cache: Shared per-run Yahoo cache.
        since: Earliest ex-date to record.
        deadline: ``time.monotonic()`` value after which the remaining
            tickers are skipped instead of fetched.

    Returns:
        (new_rows, notes, skipped) — rows to insert, log notes, and the tickers
        whose check could not run.
    """
    window = pd.Timedelta(days=CA_MATCH_DAYS)
    recorded = normalize_corporate_actions(stored).groupby("ticker")["ex_date"].agg(list).to_dict()
    new_rows: list[dict] = []
    notes: list[str] = []
    skipped: list[str] = []
    for i, tk in enumerate(tickers):
        if time.monotonic() >= deadline:
            skipped += tickers[i:]
            notes.append(f"corporate-action check skipped for {len(tickers) - i} ticker(s): fetch budget spent")
            break
        try:
            _, actions = yahoo_cached(tk, since, yahoo_cache)
        except FETCH_ERRORS as exc:
            skipped.append(tk)
            notes.append(f"{tk}: corporate-action check skipped ({type(exc).__name__})")
            continue
        added: list[pd.Timestamp] = []
        for ev in actions[actions["ex_date"] >= since].sort_values("ex_date").itertuples(index=False):
            if any(abs(ev.ex_date - day) <= window for day in recorded.get(tk, [])):
                continue
            twin = next((day for day in added if abs(ev.ex_date - day) <= window), None)
            if twin is not None:
                notes.append(f"{tk}: Yahoo event ex {ev.ex_date.date()} taken as the one ex {twin.date()} — not added twice")
                continue
            added.append(ev.ex_date)
            new_rows.append(
                {
                    "ticker": tk,
                    "ex_date": ev.ex_date,
                    "ratio": float(ev.ratio),
                    "action_type": classify_action(float(ev.ratio)),
                    "source": "yahoo",
                }
            )
    return pd.DataFrame(new_rows), notes, skipped


def disagreements(observed: pd.DataFrame, stored: pd.DataFrame) -> pd.DataFrame:
    """Provider prints that contradict a stored real print by >2%."""
    if observed.empty or stored.empty:
        return pd.DataFrame(columns=["ticker", "date"])
    both = observed[["ticker", "date", "price"]].merge(
        printed_prices(normalize_prices(stored))[["ticker", "date", "price"]],
        on=["ticker", "date"],
        suffixes=("_new", "_stored"),
    )
    rel = (both["price_new"] / both["price_stored"] - 1).abs()
    return both[rel > DISAGREEMENT_THRESHOLD]


def main() -> int:
    """Run one sync. Returns the process exit code."""
    load_dotenv()
    now = datetime.now(B3_TZ)
    deadline = time.monotonic() + FETCH_BUDGET_S
    target = last_completed_session(B3_CALENDAR, now)
    ca_since = target - pd.Timedelta(days=CA_LOOKBACK_DAYS)
    brapi_token = os.environ.get("BRAPI_KEY")
    print(f"[prices] target session {target.date()} (São Paulo now {now:%Y-%m-%d %H:%M})")

    with db.connect() as conn:
        ledger = db.load_ledger(conn)
    first = pd.to_datetime(ledger["trades"]["date"]).min()
    b3 = sessions(B3_CALENDAR, first, target)
    stored = ledger["prices"]
    actions = normalize_corporate_actions(ledger["corporate_actions"])

    required = required_pairs(ledger, b3)
    missing = missing_pairs(required, stored)
    upgrade = upgradable_pairs(required, stored)
    print(
        f"[prices] required ticker-sessions: {len(required)} | missing: {len(missing)} "
        f"| fallback rows to upgrade: {len(upgrade)}"
    )

    # 1) Official B3 files (missing + upgradable pairs share the per-session download).
    official, no_trade_all, observed_b3, notes = fetch_official(pd.concat([missing, upgrade]))
    key = ["ticker", "date"]
    official_new = anti_join(official, upgrade, key)
    official_upgrade = official.merge(upgrade[key], on=key)
    no_trade = no_trade_all.merge(missing, on=key)
    demote = no_trade_all.merge(upgrade[key], on=key)  # fallback closes on sessions B3 shows untraded

    # 2) Fallback providers for whatever B3 could not settle, within the fetch budget.
    remaining = anti_join(anti_join(missing, official_new, key), no_trade, key)
    yahoo_cache: YahooCache = {}
    fallback_parts: list[pd.DataFrame] = []
    observed_parts: list[pd.DataFrame] = [observed_b3]
    gaps = list(remaining.groupby("ticker"))
    for i, (tk, grp) in enumerate(gaps):
        if time.monotonic() >= deadline:
            notes.append(f"fetch budget spent — {len(gaps) - i} ticker(s) left unfilled for the next run")
            break
        rows, seen, n = fetch_fallback(tk, pd.DatetimeIndex(grp["date"]), b3, brapi_token, yahoo_cache, ca_since)
        fallback_parts.append(rows)
        observed_parts.append(seen)
        notes += n
    fallback = _concat(fallback_parts)

    # 3) No-trade markers carry the last real print, corporate-action adjusted. A
    #    demoted fallback close is not a print, so nothing is carried from it.
    known = _concat([anti_join(normalize_prices(stored), demote, key), official_new, fallback, official_upgrade])
    prints_known = printed_prices(normalize_prices(known))
    markers = no_trade.assign(price=carry_prices(no_trade, prints_known, actions), source=NO_TRADE_SOURCE)
    unpriced_markers = markers[markers["price"].isna()]
    markers = markers.dropna(subset=["price"])
    demoted = demote.assign(price=carry_prices(demote, prints_known, actions), source=NO_TRADE_SOURCE)
    kept_fallback = demoted[demoted["price"].isna()]  # nothing earlier to carry: the provider close stays
    demoted = demoted.dropna(subset=["price"])

    inserts = _concat([official_new, fallback, markers])
    inserts = inserts.assign(price=inserts["price"].astype(float).round(PRICE_DECIMALS))
    official_upgrade = official_upgrade.assign(price=official_upgrade["price"].astype(float).round(PRICE_DECIMALS))
    demoted = demoted.assign(price=demoted["price"].astype(float).round(PRICE_DECIMALS))

    # 4) Corporate actions for anything held since the lookback start: a split on
    #    a since-sold position still changes the share count its SELL used.
    recently_held = sorted(required.loc[required["date"] >= ca_since, "ticker"].unique())
    new_actions, ca_notes, ca_skipped = sync_corporate_actions(
        recently_held, ledger["corporate_actions"], yahoo_cache, ca_since, deadline
    )
    notes += ca_notes

    conflict = anti_join(disagreements(_concat(observed_parts), stored), upgrade, key)
    with db.connect() as conn:
        inserted = db.upsert(conn, "prices", inserts.assign(created_at=now.date()), key=key, update=False)
        upgraded = db.upsert(conn, "prices", official_upgrade[PRICE_COLUMNS], key=key, update=True)
        marked = db.upsert(conn, "prices", demoted[PRICE_COLUMNS], key=key, update=True)
        ca_inserted = db.upsert(conn, "corporate_actions", new_actions, key=["ticker", "ex_date"], update=False)

    by_source = inserts["source"].value_counts().to_dict()
    print(f"[prices] inserted {inserted} rows {by_source} | upgraded to official: {upgraded} "
          f"| fallback → no-trade: {marked} | new corporate actions: {ca_inserted}")
    for r in demoted.itertuples():
        print(f"[prices] {r.ticker} {r.date.date()}: B3 file shows no trade — fallback close replaced by a no-trade marker")
    for r in kept_fallback.itertuples():
        print(f"[prices] {r.ticker} {r.date.date()}: B3 file shows no trade but no earlier print to carry — fallback close kept")
    for line in notes:
        print(f"[prices] {line}")
    for r in conflict.itertuples():
        print(f"[prices] WARNING {r.ticker} {r.date.date()}: provider close differs >2% from stored — kept stored")

    filled = set(zip(inserts["ticker"], inserts["date"]))
    unfilled_latest = sorted(
        tk for tk, d in zip(missing["ticker"], missing["date"]) if d == target and (tk, d) not in filled
    )
    on_fallback = anti_join(anti_join(upgrade, official_upgrade, key), demoted, key)
    target_fallback = int((on_fallback["date"] == target).sum() + (fallback["date"] == target).sum())

    alert_lines: list[str] = []
    if unfilled_latest:
        alert_lines.append(
            f"⚠️ <b>No close for {target.date()}</b>: {escape(', '.join(unfilled_latest))}\n"
            "Valuation carries the previous close until a source has the bar."
        )
    if not unpriced_markers.empty:
        alert_lines.append(
            "🚨 <b>Held with no print ever</b>: "
            f"{escape(', '.join(sorted(unpriced_markers['ticker'].unique())))} — add a price manually"
        )
    if target_fallback:
        alert_lines.append(
            f"🕒 <b>B3 file for {target.date()} unavailable</b>: {target_fallback} close(s) from Yahoo/BRAPI.\n"
            f"{len(on_fallback) + len(fallback)} fallback close(s) await the official file; later runs swap them in."
        )
    if ca_skipped:
        alert_lines.append(
            f"⚠️ <b>Corporate-action check skipped</b>: {escape(', '.join(ca_skipped))}\n"
            "A split or bonus there goes unrecorded until a run completes the check — see run log."
        )
    if not new_actions.empty:
        listed = "\n".join(
            f"• {escape(r.ticker)} ×{r.ratio:g} ex {pd.Timestamp(r.ex_date).date()}" for r in new_actions.itertuples()
        )
        alert_lines.append(f"🧾 <b>New corporate actions applied</b> — verify vs broker:\n{listed}")
    if not conflict.empty:
        alert_lines.append(f"🔍 {len(conflict)} stored close(s) differ >2% from a provider — see run log")
    if alert_lines:
        send_telegram("\n\n".join(alert_lines))

    return 1 if unfilled_latest or not unpriced_markers.empty else 0


if __name__ == "__main__":
    sys.exit(main())
