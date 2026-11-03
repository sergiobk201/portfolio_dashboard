"""
calculate_daily_twr.py
──────────────────────
Daily full recompute of ``portfolio_performance`` from the ledger.

Every run rebuilds the whole series (trades + income + raw closes + corporate
actions → positions → cash → value → TWR) with the vectorized engine in
``portfolio_engine``, validates it, and writes only the rows that changed.
There is no anchor row and no rolling window: a late dividend, a trade
entered weeks after the fact or a corrected close flows into every affected
day, and the table always equals what the ledger implies.

Write policy
  • Any engine ERROR (unpriced holding, negative position, unknown side)
    blocks every write and exits 1 — a wrong number is worse than a late one.
  • Only changed rows are upserted, with updated_at = now(), so updated_at is
    an audit trail of revisions.
  • Rows outside the recomputed range are deleted (e.g. a corrected first
    trade date). If the series would lose more than MAX_SHRINK_DAYS at the
    end, the run aborts instead — that means missing data, not a correction.

Public-log hygiene: the repository and its Actions logs are public, so stdout
carries dates, counts and percentages only. Amounts go to Telegram.

Requires sql/001_integrity_corporate_actions.sql (``is_trading_day``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from dotenv import load_dotenv

import db
from alerts import escape, send_telegram
from market_data import B3_CALENDAR, B3_TZ, sessions, yesterday
from portfolio_engine import PERFORMANCE_COLUMNS, EngineResult, Issue, run_engine

TABLE = "portfolio_performance"
MONEY_COLUMNS = ["portfolio_value", "equity_value", "uninvested_cash", "total_invested", "external_inflow"]
RETURN_COLUMNS = ["daily_return", "twr"]
MONEY_DECIMALS = 4  # NUMERIC(18,4)
RETURN_DECIMALS = 8  # NUMERIC(18,8)
REVISION_GRACE_DAYS = 7  # changes this close to the previous end are routine (late closes)
MAX_SHRINK_DAYS = 7
STORED_QUERY = f"SELECT {', '.join(PERFORMANCE_COLUMNS)} FROM {TABLE}"


@dataclass
class SyncPlan:
    """What a run would write."""

    upserts: pd.DataFrame  # new or changed rows, PERFORMANCE_COLUMNS
    deletes: list[pd.Timestamp]  # stored dates outside the recomputed range
    changed: int  # existing rows whose values changed
    revised_from: pd.Timestamp | None  # earliest changed date before the grace window
    previous_end: pd.Timestamp | None  # last stored date before this run


def for_storage(perf: pd.DataFrame) -> pd.DataFrame:
    """Round to the column scales so comparisons match what the DB holds."""
    out = perf[PERFORMANCE_COLUMNS].copy()
    out[MONEY_COLUMNS] = out[MONEY_COLUMNS].astype(float).round(MONEY_DECIMALS)
    out[RETURN_COLUMNS] = out[RETURN_COLUMNS].astype(float).round(RETURN_DECIMALS)
    out["date"] = pd.to_datetime(out["date"]).dt.normalize().astype("datetime64[ns]")
    out["is_trading_day"] = out["is_trading_day"].astype(bool)
    return out.reset_index(drop=True)


def plan_sync(new: pd.DataFrame, stored: pd.DataFrame) -> SyncPlan:
    """Diff the recomputed series against the table.

    Args:
        new: Output of :func:`for_storage` for this run.
        stored: Current table rows (same columns; may be empty).

    Returns:
        SyncPlan with exactly the rows that need writing.
    """
    if stored.empty:
        return SyncPlan(new, [], 0, None, None)
    old = for_storage(stored.assign(is_trading_day=stored["is_trading_day"].eq(True)))
    merged = new.merge(old, on="date", how="left", suffixes=("", "_old"), indicator=True)

    is_new = merged["_merge"] == "left_only"
    tol = 0.5 * 10.0 ** -np.array([MONEY_DECIMALS] * len(MONEY_COLUMNS) + [RETURN_DECIMALS] * len(RETURN_COLUMNS))
    numeric = merged[MONEY_COLUMNS + RETURN_COLUMNS].to_numpy(dtype=float)
    numeric_old = merged[[f"{c}_old" for c in MONEY_COLUMNS + RETURN_COLUMNS]].to_numpy(dtype=float)
    value_changed = (np.abs(numeric - numeric_old) > tol).any(axis=1)
    flag_changed = merged["is_trading_day"].to_numpy() != merged["is_trading_day_old"].eq(True).to_numpy()
    changed = ~is_new & (value_changed | flag_changed)

    previous_end = old["date"].max()
    revised = merged.loc[changed & (merged["date"] < previous_end - pd.Timedelta(days=REVISION_GRACE_DAYS)), "date"]
    deletes = sorted(set(old["date"]) - set(new["date"]))
    return SyncPlan(
        upserts=merged.loc[is_new | changed, PERFORMANCE_COLUMNS].reset_index(drop=True),
        deletes=deletes,
        changed=int(changed.sum()),
        revised_from=revised.min() if not revised.empty else None,
        previous_end=previous_end,
    )


def compute(ledger: dict[str, pd.DataFrame], end: pd.Timestamp) -> EngineResult:
    """Run the engine over the full ledger through ``end``."""
    first = pd.to_datetime(ledger["trades"]["date"]).min()
    b3 = sessions(B3_CALENDAR, first, end)
    return run_engine(
        ledger["trades"],
        ledger["dividends"],
        ledger["prices"],
        ledger["corporate_actions"],
        b3,
        end,
    )


def apply_plan(conn: db.PgConnection, plan: SyncPlan) -> tuple[int, int]:
    """Write a SyncPlan inside the caller's transaction → (upserted, deleted)."""
    written = db.upsert(
        conn,
        TABLE,
        plan.upserts,
        key=["date"],
        update=True,
        touch_column="updated_at",
    )
    deleted = 0
    if plan.deletes:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {TABLE} WHERE date = ANY(%s)",
                ([d.date() for d in plan.deletes],),
            )
            deleted = cur.rowcount
    return written, deleted


def shrink_days(plan: SyncPlan, new: pd.DataFrame) -> int:
    """Calendar days the series end would move backwards."""
    if plan.previous_end is None:
        return 0
    return max(0, (plan.previous_end - new["date"].max()).days)


def _issue_lines(issues: list[Issue], limit: int = 15) -> str:
    lines = [f"• <code>{escape(i.code)}</code> {escape(i.message)}" for i in issues[:limit]]
    if len(issues) > limit:
        lines.append(f"…and {len(issues) - limit} more")
    return "\n".join(lines)


def _brl(value: float) -> str:
    return f"R$ {value:,.2f}"


def build_alerts(result: EngineResult, new: pd.DataFrame, stored: pd.DataFrame, plan: SyncPlan) -> list[str]:
    """Telegram messages worth a human look (amounts allowed: private channel)."""
    alerts: list[str] = []
    fresh_after = plan.previous_end if plan.previous_end is not None else pd.Timestamp.min
    fresh = [i for i in result.warnings if i.code == "TRUNCATED_END" or (i.date is not None and i.date > fresh_after)]
    if fresh:
        alerts.append(f"⚠️ <b>New data warnings</b>\n{_issue_lines(fresh)}")

    if plan.revised_from is not None and not stored.empty:
        old_end = stored.loc[pd.to_datetime(stored["date"]) == plan.previous_end]
        new_end = new.loc[new["date"] == plan.previous_end]
        if not old_end.empty and not new_end.empty:
            o, n = old_end.iloc[0], new_end.iloc[0]
            alerts.append(
                f"📝 <b>History revised from {plan.revised_from.date()}</b> "
                f"({plan.changed} existing rows changed)\n"
                f"As of {plan.previous_end.date()}: TWR {float(o['twr']):+.2%} → {float(n['twr']):+.2%}, "
                f"value {_brl(float(o['portfolio_value']))} → {_brl(float(n['portfolio_value']))}\n"
                "Cause: a ledger, price or corporate-action change. Verify it was intended."
            )

    sessions_only = new[new["is_trading_day"]]
    if len(sessions_only) >= 2:
        prev, last = sessions_only.iloc[-2], sessions_only.iloc[-1]
        crossed = np.sign(prev["twr"]) != np.sign(last["twr"]) and last["date"] > fresh_after
        if crossed:
            alerts.append(f"🔁 Cumulative TWR crossed zero on {last['date'].date()}: {last['twr']:+.2%}")
    return alerts


def main() -> int:
    """Recompute, validate and sync. Returns the process exit code."""
    load_dotenv()
    now = datetime.now(B3_TZ)
    end = yesterday(B3_TZ, now)
    print(f"[twr] recompute through {end.date()} (São Paulo now {now:%Y-%m-%d %H:%M})")

    with db.connect() as conn:
        ledger = db.load_ledger(conn)
        stored = db.read_frame(conn, STORED_QUERY)

    result = compute(ledger, end)
    counts = pd.Series([i.code for i in result.issues], dtype=str).value_counts().to_dict()
    print(f"[twr] issues: {counts or 'none'}")

    if not result.ok:
        for i in result.errors:
            print(f"[twr] ERROR {i.code}: {i.message}")
        send_telegram(f"🚨 <b>TWR job blocked — nothing written</b>\n{_issue_lines(result.errors)}")
        return 1

    new = for_storage(result.performance)
    plan = plan_sync(new, stored)
    shrink = shrink_days(plan, new)
    if shrink > MAX_SHRINK_DAYS:
        msg = f"series would end {shrink} days earlier than the stored one — refusing to delete"
        print(f"[twr] ERROR {msg}")
        send_telegram(f"🚨 <b>TWR job blocked</b>: {escape(msg)}")
        return 1

    with db.connect() as conn:
        written, deleted = apply_plan(conn, plan)

    last = new.iloc[-1]
    span = f"{new['date'].iloc[0].date()} → {last['date'].date()}"
    print(f"[twr] series {span}: {len(new)} days, {int(new['is_trading_day'].sum())} sessions")
    revised = plan.revised_from.date() if plan.revised_from is not None else "no"
    print(f"[twr] upserted {written} row(s) ({plan.changed} changed), deleted {deleted} | revised history: {revised}")
    print(f"[twr] cumulative TWR {last['twr']:+.4%} | last daily return {last['daily_return']:+.4%}")

    alerts = build_alerts(result, new, stored, plan)
    if alerts:
        send_telegram("\n\n".join(alerts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
