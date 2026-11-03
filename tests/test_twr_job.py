"""Diff-only sync of portfolio_performance."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from calculate_daily_twr import REVISION_GRACE_DAYS, for_storage, plan_sync, shrink_days
from portfolio_engine import PERFORMANCE_COLUMNS

DAYS = pd.date_range("2025-01-01", periods=40, freq="D")


def series(days: pd.DatetimeIndex = DAYS) -> pd.DataFrame:
    n = len(days)
    value = 1000.0 + np.arange(n) * 1.123456789
    return pd.DataFrame(
        {
            "date": days,
            "portfolio_value": value,
            "equity_value": value - 10.0,
            "uninvested_cash": np.full(n, 10.0),
            "total_invested": np.full(n, 1000.0),
            "external_inflow": np.r_[1000.0, np.zeros(n - 1)],
            "daily_return": np.r_[0.0, np.diff(value) / value[:-1]],
            "twr": value / 1000.0 - 1.0,
            "is_trading_day": days.dayofweek < 5,
        }
    )[PERFORMANCE_COLUMNS]


def as_db(frame: pd.DataFrame) -> pd.DataFrame:
    """What psycopg2 returns: NUMERIC → Decimal, DATE → datetime.date."""
    out = for_storage(frame).astype(object)
    for col in PERFORMANCE_COLUMNS[1:-1]:
        out[col] = [Decimal(str(v)) for v in out[col]]
    out["date"] = [d.date() for d in pd.to_datetime(out["date"])]
    return out


def test_first_run_writes_everything():
    new = for_storage(series())
    plan = plan_sync(new, pd.DataFrame(columns=PERFORMANCE_COLUMNS))
    assert len(plan.upserts) == len(DAYS) and plan.deletes == [] and plan.previous_end is None


def test_unchanged_history_is_a_no_op():
    plan = plan_sync(for_storage(series()), as_db(series()))
    assert plan.upserts.empty and plan.deletes == [] and plan.changed == 0 and plan.revised_from is None


def test_float_noise_below_column_scale_is_ignored():
    noisy = series()
    noisy["portfolio_value"] += 1e-7
    noisy["twr"] += 1e-11
    assert plan_sync(for_storage(noisy), as_db(series())).upserts.empty


def test_only_new_days_are_appended():
    stored = as_db(series(DAYS[:-2]))
    plan = plan_sync(for_storage(series()), stored)
    assert plan.upserts["date"].tolist() == list(DAYS[-2:]) and plan.changed == 0


def test_old_revision_is_flagged_and_recent_one_is_routine():
    old = series()
    old.loc[3, "portfolio_value"] += 5.0
    plan = plan_sync(for_storage(old), as_db(series()))
    assert plan.changed == 1 and plan.revised_from == DAYS[3]

    recent = series()
    recent.loc[len(DAYS) - REVISION_GRACE_DAYS, "twr"] += 0.001
    plan = plan_sync(for_storage(recent), as_db(series()))
    assert plan.changed == 1 and plan.revised_from is None


def test_legacy_null_trading_day_flag_is_rewritten():
    stored = as_db(series())
    stored["is_trading_day"] = None  # rows written before the column existed
    plan = plan_sync(for_storage(series()), stored)
    assert plan.changed == int(series()["is_trading_day"].sum())


def test_days_outside_the_recompute_are_deleted_and_shrink_is_measured():
    new = for_storage(series(DAYS[2:-3]))
    plan = plan_sync(new, as_db(series()))
    assert plan.deletes == list(DAYS[:2]) + list(DAYS[-3:])
    assert shrink_days(plan, new) == 3
