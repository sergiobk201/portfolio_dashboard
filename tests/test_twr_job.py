"""Diff-only sync of portfolio_performance."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from calculate_daily_twr import (
    REVISION_GRACE_DAYS,
    build_digest,
    for_storage,
    format_digest,
    plan_sync,
    shrink_days,
)
from portfolio_engine import PERFORMANCE_COLUMNS

DAYS = pd.date_range("2025-01-01", periods=40, freq="D")  # Wed 2025-01-01 … Sun 2025-02-09


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


# ── Daily digest ─────────────────────────────────────────────────────────────


def _by_date(frame: pd.DataFrame, column: str) -> pd.Series:
    return frame.set_index("date")[column]


def test_digest_is_sent_only_when_a_session_is_appended():
    full = for_storage(series())
    assert build_digest(full, plan_sync(full, as_db(series()))) is None  # retry: nothing new
    assert build_digest(full, plan_sync(full, as_db(series(DAYS[:-1])))) is None  # appends Sunday only


def test_digest_reports_the_appended_session_as_of_its_close():
    # A Sunday run after a failed Saturday run: appends Fri, Sat and Sun.
    fri, thu = DAYS[-3], DAYS[-4]
    perf = series()
    perf.loc[perf["date"] == fri, "external_inflow"] = 250.0  # money put in on Friday
    new = for_storage(perf)
    d = build_digest(new, plan_sync(new, as_db(series(DAYS[:-3]))))
    twr, value = _by_date(new, "twr"), _by_date(new, "portfolio_value")

    assert (d.session, d.base, d.sessions) == (fri, thu, 1)
    assert d.period_return == pytest.approx((1 + twr[fri]) / (1 + twr[thu]) - 1)
    assert d.pnl == pytest.approx(value[fri] - value[thu] - 250.0)
    assert (d.itd, d.value) == (pytest.approx(twr[fri]), pytest.approx(value[fri]))  # not Sunday's
    lines = format_digest(d).splitlines()
    assert lines[0] == "📊 <b>Portfolio — Fri 2025-02-07</b>" and lines[1].startswith("Day: <b>")


def test_catch_up_digest_spans_every_appended_session():
    days = DAYS[:-3]  # … Tue 02-04, Wed 02-05, Thu 02-06
    new = for_storage(series(days))
    d = build_digest(new, plan_sync(new, as_db(series(days[:-2]))))
    twr = _by_date(new, "twr")
    assert (d.session, d.base, d.sessions) == (days[-1], days[-3], 2)
    assert d.period_return == pytest.approx((1 + twr[days[-1]]) / (1 + twr[days[-3]]) - 1)
    assert "Since 2025-02-04 (2 sessions)" in format_digest(d)


def test_mtd_and_ytd_start_from_the_prior_period_close():
    # The synthetic level moves every calendar day, so an off-by-one base fails.
    days = pd.date_range("2024-12-20", "2025-02-07", freq="D")
    new = for_storage(series(days))
    d = build_digest(new, plan_sync(new, as_db(series(days[:-1]))))
    level = 1 + _by_date(new, "twr")
    assert d.mtd == pytest.approx(level[days[-1]] / level[pd.Timestamp("2025-01-31")] - 1)
    assert d.ytd == pytest.approx(level[days[-1]] / level[pd.Timestamp("2024-12-31")] - 1)


def test_first_run_digest_covers_the_whole_history():
    new = for_storage(series(DAYS[:-2]))
    d = build_digest(new, plan_sync(new, pd.DataFrame(columns=PERFORMANCE_COLUMNS)))
    assert d.base is None and d.period_return == pytest.approx(d.itd)
    assert d.pnl == pytest.approx(new["portfolio_value"].iloc[-1] - 1000.0)  # net of the opening inflow
    assert format_digest(d).splitlines()[1].startswith("Since inception: <b>")
