"""Backfill price plan, investor statistics and the IRPF view."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backfill_history import (
    calendar_year_returns,
    irpf_asset_class,
    monthly_tax_view,
    plan_corporate_actions,
    plan_prices,
)
from builders import SESSIONS as S
from builders import actions_frame, prices_frame, trades_frame
from portfolio_engine import normalize_corporate_actions
from prices_pipeline import _pairs

SATURDAY = S[4] + pd.Timedelta(days=1)
NO_ACTIONS = normalize_corporate_actions(None)


def official(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["ticker", "date", "price"])


@pytest.fixture
def scenario() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    stored = prices_frame(
        [
            (S[0], "AAAA3", 9.90, "legacy"),  # wrong value (dividend-adjusted)
            (S[1], "AAAA3", 10.10, "legacy"),  # right value, unverified source
            (S[2], "AAAA3", 10.10, "yahoo_raw"),  # carried quote: B3 shows no trade
            (SATURDAY, "AAAA3", 10.10, "legacy"),  # not a session
            (S[1], "BBBB4", 5.00, "legacy"),  # not held, no print: provider artefact
            (S[2], "BBBB4", 5.00, "legacy"),  # not held but officially printed: keep, relabel
        ]
    )
    off = official(
        [("AAAA3", S[0], 10.00), ("AAAA3", S[1], 10.10), ("AAAA3", S[3], 10.30), ("BBBB4", S[2], 5.00)]
    )
    held = _pairs([("AAAA3", d) for d in S[:5]])
    return stored, off, held


def test_plan_prices_aligns_every_row_to_the_official_close(scenario):
    stored, off, held = scenario
    plan = plan_prices(stored, off, held, S[:5], NO_ACTIONS)
    assert plan.stats == {
        "stored_rows": 6,
        "deleted_non_session": 1,
        "deleted_no_print_not_held": 1,
        "updated_to_official": 3,
        "updated_value_changed": 1,
        "legacy_rows_off_by_more_than_2pct": 0,
        "inserted_official": 1,
        "no_trade_markers_new": 1,
        "no_trade_markers_repriced": 1,
        "held_pairs_unpriceable": 0,
        "final_rows": 6,
    }
    final = plan.final.set_index(["ticker", "date"]).sort_index()
    assert final.loc[("AAAA3", S[0])].tolist() == [10.00, "b3_cotahist"]
    assert final.loc[("AAAA3", S[2])].tolist() == [10.10, "b3_no_trade"]
    assert final.loc[("AAAA3", S[3])].tolist() == [10.30, "b3_cotahist"]
    assert final.loc[("AAAA3", S[4])].tolist() == [10.30, "b3_no_trade"]
    assert final.loc[("BBBB4", S[2])].tolist() == [5.00, "b3_cotahist"]
    assert ("BBBB4", S[1]) not in final.index and ("AAAA3", SATURDAY) not in final.index


def test_plan_prices_converges(scenario):
    stored, off, held = scenario
    first = plan_prices(stored, off, held, S[:5], NO_ACTIONS)
    again = plan_prices(first.final, off, held, S[:5], NO_ACTIONS)
    assert again.deletes.empty and again.updates.empty and again.inserts.empty


def test_markers_after_a_split_carry_the_adjusted_close(scenario):
    stored, off, held = scenario
    ca = normalize_corporate_actions(actions_frame([("AAAA3", S[4], 2.0)]))
    plan = plan_prices(stored, off, held, S[:5], ca)
    marker = plan.inserts.set_index(["ticker", "date"]).loc[("AAAA3", S[4])]
    assert marker.tolist() == [5.15, "b3_no_trade"]


def test_calendar_year_returns_chain_to_the_total():
    dates = pd.date_range("2024-06-17", "2026-03-31", freq="D")
    daily = np.random.default_rng(3).normal(0.0004, 0.01, len(dates))
    twr = np.cumprod(1 + daily) - 1
    years = calendar_year_returns(pd.DataFrame({"date": dates, "twr": twr}))
    assert years.index.tolist() == [2024, 2025, 2026]
    assert years[2024] == pytest.approx(twr[dates.year == 2024][-1])  # first year starts at inception
    assert float(np.prod(1 + years) - 1) == pytest.approx(twr[-1])


def test_irpf_asset_classes():
    spec = pd.Series(["AMBEV S/A ON", "TAESA UNT N2", "APPLE DRN", "ISHARES BOVA CI", "FII XP LOG CI", "X"])
    tick = pd.Series(["ABEV3", "TAEE11", "AAPL34", "BOVA11", "XPLG11", "MSFT34"])
    assert irpf_asset_class(spec, tick).tolist() == ["ACOES", "ACOES", "BDR", "ETF", "FII", "BDR"]


def test_monthly_tax_view_separates_exempt_and_taxable_sales():
    t = trades_frame(
        [
            ("2025-01-10", "BUY", "AAAA3", 100, 10.0),
            ("2025-02-10", "SELL", "AAAA3", 100, 12.0),  # R$1,200 of shares → exempt
            ("2025-01-10", "BUY", "AAPL34", 10, 50.0),
            ("2025-02-11", "SELL", "AAPL34", 10, 60.0),  # BDR → always taxable
            ("2025-03-12", "BUY", "CCCC3", 10, 10.0),
            ("2025-03-12", "SELL", "CCCC3", 10, 11.0),  # day trade → never auto-exempt
        ]
    )
    t.loc[t["ticker"] == "AAPL34", "asset"] = "APPLE DRN"
    out = monthly_tax_view({"trades": t}, actions_frame([])).set_index(["month", "asset_class"])
    assert out.loc[("2025-02", "ACOES"), "realised_pnl"] == pytest.approx(200.0)
    assert bool(out.loc[("2025-02", "ACOES"), "exempt_swing"])
    assert out.loc[("2025-02", "BDR"), "realised_pnl"] == pytest.approx(100.0)
    assert not bool(out.loc[("2025-02", "BDR"), "exempt_swing"])
    assert out.loc[("2025-03", "ACOES"), "day_trades"] == 1
    assert not bool(out.loc[("2025-03", "ACOES"), "exempt_swing"])


def test_planned_corporate_actions_match_recorded_ones_within_the_window():
    day = pd.Timestamp
    stored = actions_frame([("AAAA3", day("2025-03-12"), 1.0)])  # rejected by hand: ratio = 1
    yahoo = pd.DataFrame(
        {
            "ticker": ["AAAA3", "AAAA3", "BBBB4", "BBBB4", "CYRE3"],
            "ex_date": [day("2025-03-02"), day("2025-09-01"), day("2025-05-09"), day("2025-05-05"), day("2025-12-30")],
            "ratio": [5.0, 2.0, 1.1, 1.1, 1.18],
        }
    )
    plan = plan_corporate_actions(stored, yahoo)
    # AAAA3 03-02 is the rejected 03-12 row (10 days), BBBB4 is one event (the earliest
    # date), and CYRE3 on its data-com is the manual 2026-01-02 bonus, which wins.
    assert list(zip(plan["ticker"], plan["ex_date"], plan["source"])) == [
        ("BBBB4", day("2025-05-05"), "yahoo"),
        ("AAAA3", day("2025-09-01"), "yahoo"),
        ("CYRE3", day("2026-01-02"), "manual"),
    ]
    assert plan_corporate_actions(stored, yahoo.assign(ex_date=yahoo["ex_date"] - pd.Timedelta(days=1)))[
        "ticker"
    ].tolist().count("AAAA3") == 2  # 11 days from the rejected row: a different event
