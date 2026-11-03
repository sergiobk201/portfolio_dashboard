"""
spy_pipeline.py
───────────────
Self-healing sync of the SPY benchmark: one RAW close per NYSE session.

Every run diffs the NYSE sessions since the portfolio's first trade against
``benchmark`` and fetches only what is missing, so a skipped or failed run is
repaired by the next one.

Layers: FMP end-of-day history (explicitly dated) → Yahoo raw.
  • Each close is stored under the session it belongs to. The previous job
    stored quote.previousClose as "yesterday", which mislabelled the session
    whenever it ran on a Saturday, a Monday or after a holiday.
  • Existing rows are never overwritten; a provider disagreeing with a stored
    close by more than DISAGREEMENT_THRESHOLD is reported, not applied.
  • No placeholder prices: an unfilled latest session alerts and exits 1.

Requires sql/001_integrity_corporate_actions.sql (UNIQUE(date) and
``benchmark.source``).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

import db
from alerts import send_telegram
from market_data import (
    FETCH_ERRORS,
    NY_TZ,
    NYSE_CALENDAR,
    fmp_history,
    last_completed_session,
    sessions,
    yahoo_raw_history,
)

SYMBOL = "SPY"
DISAGREEMENT_THRESHOLD = 0.002  # official closes agree to the cent; 0.2% = wrong session
PRICE_DECIMALS = 2  # benchmark.price is NUMERIC(10,2)


def stored_closes(benchmark: pd.DataFrame) -> pd.DataFrame:
    """Typed benchmark rows (date, price), one per date."""
    out = benchmark[["date", "price"]].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize().astype("datetime64[ns]")
    out["price"] = out["price"].astype(float)
    return out.drop_duplicates("date", keep="last")


def fetch_closes(
    missing: pd.DatetimeIndex,
    nyse_sessions: pd.DatetimeIndex,
    fmp_key: str | None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Fill missing sessions: FMP first, then Yahoo raw.

    Returns:
        (rows[date, price, source], observed[date, price], notes).
    """
    wanted = set(missing)
    found: list[pd.DataFrame] = []
    observed: list[pd.DataFrame] = []
    notes: list[str] = []

    if fmp_key:
        try:
            bars = fmp_history(SYMBOL, fmp_key, min(wanted).date(), max(wanted).date())
            observed.append(bars)
            hit = bars[bars["date"].isin(wanted)]
            found.append(hit.assign(source="fmp"))
            wanted -= set(hit["date"])
        except FETCH_ERRORS as exc:
            notes.append(f"FMP failed ({type(exc).__name__}) — falling back to Yahoo")
    else:
        notes.append("FMP_API_KEY not set — using Yahoo")

    if wanted:
        try:
            bars, _ = yahoo_raw_history(SYMBOL, min(wanted) - pd.Timedelta(days=7), market="US")
            observed.append(bars[["date", "price"]])
            hit = bars[bars["date"].isin(wanted)][["date", "price"]]
            found.append(hit.assign(source="yahoo_raw"))
            wanted -= set(hit["date"])
        except FETCH_ERRORS as exc:
            notes.append(f"Yahoo failed ({type(exc).__name__})")

    def _frame(parts: list[pd.DataFrame], cols: list[str]) -> pd.DataFrame:
        out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
        out = out[out["date"].isin(nyse_sessions)]
        return out.assign(price=out["price"].astype(float).round(PRICE_DECIMALS))

    rows = _frame(found, ["date", "price", "source"]).drop_duplicates("date")
    seen = _frame(observed, ["date", "price"]).drop_duplicates("date")
    if wanted:
        notes.append(f"{len(wanted)} session(s) unfilled, latest {max(wanted).date()}")
    return rows[["date", "price", "source"]], seen[["date", "price"]], notes


def main() -> int:
    """Run one sync. Returns the process exit code."""
    load_dotenv()
    now = datetime.now(NY_TZ)
    target = last_completed_session(NYSE_CALENDAR, now)
    print(f"[spy] target session {target.date()} (New York now {now:%Y-%m-%d %H:%M})")

    with db.connect() as conn:
        first = db.read_frame(conn, "SELECT MIN(date) AS first_trade FROM trades")["first_trade"].iloc[0]
        stored = stored_closes(db.read_frame(conn, "SELECT date, price FROM benchmark"))

    nyse = sessions(NYSE_CALENDAR, pd.Timestamp(first), target)
    missing = nyse.difference(pd.DatetimeIndex(stored["date"]))
    off_session = int((~stored["date"].isin(nyse) & (stored["date"] >= nyse[0])).sum())
    print(f"[spy] NYSE sessions: {len(nyse)} | missing: {len(missing)} | stored on non-sessions: {off_session}")

    rows = pd.DataFrame(columns=["date", "price", "source"])
    observed = pd.DataFrame(columns=["date", "price"])
    notes: list[str] = []
    if len(missing):
        rows, observed, notes = fetch_closes(missing, nyse, os.environ.get("FMP_API_KEY"))

    both = observed.merge(stored, on="date", suffixes=("_new", "_stored"))
    conflict = both[(both["price_new"] / both["price_stored"] - 1).abs() > DISAGREEMENT_THRESHOLD]

    with db.connect() as conn:
        inserted = db.upsert(
            conn,
            "benchmark",
            rows.assign(created_at=now.date()) if not rows.empty else rows,
            key=["date"],
            update=False,
        )

    by_source = rows["source"].value_counts().to_dict() if not rows.empty else {}
    print(f"[spy] inserted {inserted} closes {by_source}")
    for line in notes:
        print(f"[spy] {line}")
    for r in conflict.itertuples():
        print(f"[spy] WARNING {r.date.date()}: provider close differs from stored — kept stored")

    unfilled_latest = target in set(missing) and target not in set(rows["date"])
    if unfilled_latest:
        send_telegram(
            f"⚠️ <b>SPY close missing for {target.date()}</b> — FMP and Yahoo both failed.\n"
            "The next run retries automatically; no placeholder was written."
        )
    elif off_session or not conflict.empty:
        send_telegram(
            f"🔍 Benchmark check: {off_session} row(s) on non-NYSE days, "
            f"{len(conflict)} close(s) differing from the provider — run backfill_history.py"
        )
    return 1 if unfilled_latest else 0


if __name__ == "__main__":
    sys.exit(main())
