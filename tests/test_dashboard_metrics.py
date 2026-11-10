"""Dashboard metrics: corporate-action-aware holdings/YTD, session stats, BRL benchmark."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from builders import SESSIONS as S
from builders import actions_frame, prices_frame, trades_frame
from dashboard_metrics import (
    accrual_index,
    adjusted_prices,
    change_since,
    compound,
    dividend_yields,
    fold_other,
    holdings,
    income_events,
    parse_sgs,
    period_returns,
    rebased,
    risk_stats,
    segment_scorecard,
    session_returns,
    spread_labels,
    to_brl,
    total_return_index,
    weighted_ytd,
    ytd_changes,
)

SPLIT = actions_frame([("AAAA3", S[3], 5.0)])  # 1:5 split
YEAR = S[2]  # treat S[2] as Jan 1 of the reporting year
BASE = S[1]  # last B3 session before YEAR: the YTD base


def _prices() -> pd.DataFrame:
    return prices_frame(
        [(S[0], "AAAA3", 100.0), (S[1], "AAAA3", 100.0), (S[2], "AAAA3", 104.0), (S[3], "AAAA3", 21.0)]
        + [(S[i], "BBBB4", v) for i, v in enumerate([10.0, 10.0, 10.0, 11.0])]
    )


def _trades() -> pd.DataFrame:
    t = trades_frame(
        [(S[0], "BUY", "AAAA3", 100, 100.0), (S[0], "BUY", "BBBB4", 50, 10.0), (S[1], "BUY", "CCCC3", 10, 5.0)]
        + [(S[2], "SELL", "CCCC3", 10, 5.0)]
    )
    t["portfolio"] = ["Growth", "Income", "Growth", "Growth"]
    return t


def test_adjusted_prices_restate_history_in_todays_share_basis():
    adj = adjusted_prices(_prices(), SPLIT).set_index(["ticker", "date"])["adj_price"]
    assert adj[("AAAA3", S[0])] == pytest.approx(20.0)  # 100 / 5
    assert adj[("AAAA3", S[3])] == pytest.approx(21.0)  # post-split close unchanged
    assert adj[("BBBB4", S[0])] == pytest.approx(10.0)  # no event, no change


def test_ytd_is_continuous_across_a_split():
    ytd = ytd_changes(adjusted_prices(_prices(), SPLIT), YEAR, BASE)
    assert ytd["AAAA3"] == pytest.approx(5.0)  # base = last close before the year (100 → 21 × 5)
    raw = ytd_changes(adjusted_prices(_prices(), None), YEAR, BASE)
    assert raw["AAAA3"] == pytest.approx(-79.0)  # what the dashboard showed before


def test_ytd_of_a_ticker_bought_this_year_starts_at_its_first_close():
    p = prices_frame([(S[3], "DDDD3", 8.0), (S[4], "DDDD3", 10.0)])
    assert ytd_changes(adjusted_prices(p, None), YEAR, BASE)["DDDD3"] == pytest.approx(25.0)


def test_ytd_of_a_rebought_ticker_ignores_its_earlier_holding_period():
    # prices only hold sessions on which a ticker was held: CCCC3's 2024 close is
    # from an earlier holding period, not a year-end base.
    base, now = pd.Timestamp("2025-12-30"), pd.Timestamp("2026-11-06")
    p = prices_frame(
        [(base, "AAAA3", 10.0), (now, "AAAA3", 12.0)]  # held over the year end
        + [(pd.Timestamp("2024-06-03"), "CCCC3", 30.0), (pd.Timestamp("2026-06-01"), "CCCC3", 40.0), (now, "CCCC3", 45.0)]
    )
    ytd = ytd_changes(adjusted_prices(p, None), pd.Timestamp("2026-01-01"), base)
    assert ytd["AAAA3"] == pytest.approx(20.0)
    assert ytd["CCCC3"] == pytest.approx(12.5)  # from its first 2026 close, not +50 % from 2024
    assert ytd_changes(adjusted_prices(p, None), pd.Timestamp("2026-01-01"), None)["AAAA3"] == pytest.approx(0.0)


def test_holdings_apply_corporate_actions_and_drop_closed_positions():
    h = holdings(_trades(), SPLIT, adjusted_prices(_prices(), SPLIT)).set_index("ticker")
    assert h.index.tolist() == ["AAAA3", "BBBB4"]  # CCCC3 was sold
    assert h.at["AAAA3", "quantity"] == 500  # 100 × 5
    assert h.at["AAAA3", "value"] == pytest.approx(500 * 21.0)
    growth_only = holdings(_trades(), SPLIT, adjusted_prices(_prices(), SPLIT), portfolios=["Growth"])
    assert growth_only["ticker"].tolist() == ["AAAA3"]


def test_segment_scorecard_weights_and_yield():
    divs = pd.DataFrame(
        {"dividend_date": [S[3]], "ticker": ["BBBB4"], "total": [11.0], "dividend": [0.22], "portfolio": ["Income"]}
    )
    fraction = divs.assign(type="FRAÇÕES", total=99.0)  # auctioned fractions: capital, not income
    divs = pd.concat([divs.assign(type="DIVIDENDOS"), fraction], ignore_index=True)
    assert income_events(divs)["type"].tolist() == ["DIVIDENDOS"]
    card = segment_scorecard(
        _trades(), SPLIT, adjusted_prices(_prices(), SPLIT), divs, YEAR, base_session=BASE
    ).set_index("Portfolio")
    assert card["Weight %"].sum() == pytest.approx(100.0)
    assert card.at["Growth", "YTD Change"] == pytest.approx(5.0)
    assert card.at["Income", "DY %"] == pytest.approx(11.0 / (50 * 11.0) * 100)


def test_weighted_ytd_weights_by_start_of_year_value():
    ytd = pd.Series({"AAAA3": 100.0, "BBBB4": -50.0, "CCCC3": -100.0})
    equal_start = pd.DataFrame({"ticker": ["AAAA3", "BBBB4"], "value": [200.0, 50.0]})  # 100 each on Jan 1
    assert weighted_ytd(equal_start, ytd) == pytest.approx(25.0)  # basket, not the end-weighted +70 %
    wiped = pd.DataFrame({"ticker": ["AAAA3", "CCCC3"], "value": [200.0, 0.0]})  # no start value at −100 %
    assert weighted_ytd(wiped, ytd) == pytest.approx(100.0)
    assert weighted_ytd(pd.DataFrame({"ticker": ["AAAA3"], "value": [np.nan]}), ytd) == 0.0  # unpriced
    assert weighted_ytd(pd.DataFrame(columns=["ticker", "value"]), ytd) == 0.0


def test_segment_ytd_is_the_start_weighted_basket():
    # Growth holds equal year-start stakes that went +100 % and −50 %.
    p = prices_frame(
        [(BASE, "AAAA3", 10.0), (S[3], "AAAA3", 20.0), (BASE, "BBBB4", 10.0), (S[3], "BBBB4", 5.0)]
    )
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0), (S[0], "BUY", "BBBB4", 100, 10.0)]).assign(portfolio="Growth")
    no_income = pd.DataFrame(columns=["dividend_date", "ticker", "type", "total", "portfolio"])
    card = segment_scorecard(t, None, adjusted_prices(p, None), no_income, YEAR, base_session=BASE)
    assert card.set_index("Portfolio").at["Growth", "YTD Change"] == pytest.approx(25.0)


def test_dividend_yield_keys_the_split_on_the_record_date():
    # 100 shares before the 1:5 split (500 after). Row 1 was struck on the 100
    # pre-split shares but paid after the ex-date; row 2 was struck on the 500.
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 100.0)])
    divs = pd.DataFrame(
        {"dividend_date": [S[5], S[6]], "ticker": ["AAAA3", "AAAA3"], "dividend": [1.0, 0.2],
         "total": [100.0, 100.0], "quantity": [100, 500]}
    )
    dy = dividend_yields(divs, adjusted_prices(_prices(), SPLIT), SPLIT, t)
    assert dy.tolist() == pytest.approx([1.0 / 5 / 21.0 * 100, 0.2 / 21.0 * 100])
    # 100 shares also held after the ex-date (sold down to 100): ambiguous → payment-date basis.
    sold_down = pd.concat([t, trades_frame([(S[4], "SELL", "AAAA3", 400, 21.0)])], ignore_index=True)
    dy = dividend_yields(divs.iloc[:1], adjusted_prices(_prices(), SPLIT), SPLIT, sold_down)
    assert dy.tolist() == pytest.approx([1.0 / 21.0 * 100])


def test_dividend_yield_restates_pre_split_amounts():
    divs = pd.DataFrame({"dividend_date": [S[1], S[3]], "ticker": ["AAAA3", "AAAA3"], "dividend": [5.0, 1.05]})
    dy = dividend_yields(divs, adjusted_prices(_prices(), SPLIT), SPLIT)
    assert dy.tolist() == pytest.approx([5.0 / 5 / 21.0 * 100, 1.05 / 21.0 * 100])


def test_dividend_yield_uses_exact_per_share_amount():
    # dividend is stored with 2 decimals: 0.0189/share prints as 0.02 (+6 %).
    divs = pd.DataFrame(
        {"dividend_date": [S[3], S[3]], "ticker": ["BBBB4", "BBBB4"], "dividend": [0.02, 0.0],
         "total": [18.9, 0.0], "quantity": [1000, 0]}
    )
    dy = dividend_yields(divs, adjusted_prices(_prices(), None), None)
    assert dy.tolist() == pytest.approx([0.0189 / 11.0 * 100, 0.0])  # quantity 0 → falls back


def test_session_returns_fold_fill_days_and_compound_exactly():
    days = pd.date_range("2026-01-02", periods=10, freq="D")  # Fri … Sun
    perf = pd.DataFrame(
        {
            "date": days,
            "daily_return": [0.01, 0.0, 0.002, -0.02, 0.015, 0.0, 0.003, 0.0, 0.0, 0.01],
            "is_trading_day": days.dayofweek < 5,
        }
    )
    sess = session_returns(perf)
    assert list(sess.index) == list(days[days.dayofweek < 5])
    assert compound(sess) == pytest.approx(compound(perf["daily_return"]))
    assert sess.iloc[0] == pytest.approx(1.01 * 1.0 * 1.002 - 1)  # Fri + weekend


def test_risk_stats_match_textbook_formulas():
    rng = np.random.default_rng(5)
    idx = pd.bdate_range("2026-01-02", periods=200)
    r = pd.Series(rng.normal(0.0006, 0.011, len(idx)), index=idx)
    rf = pd.Series(0.0004, index=idx)
    out = risk_stats(r, rf)
    ex = r - rf
    assert out["vol"] == pytest.approx(r.std() * np.sqrt(252) * 100)
    assert out["sharpe"] == pytest.approx(ex.mean() / ex.std() * np.sqrt(252))
    assert out["sortino"] == pytest.approx(ex.mean() / np.sqrt(np.mean(np.minimum(ex, 0) ** 2)) * np.sqrt(252))
    assert risk_stats(r.iloc[:1], None)["vol"] == 0.0


def test_total_return_index_reinvests_dividends():
    idx = pd.bdate_range("2026-03-06", periods=3)  # Fri, Mon, Tue
    closes = pd.Series([100.0, 100.0, 100.0], index=idx)
    div = pd.Series([1.0], index=[pd.Timestamp("2026-03-08")])  # Sunday ex-date → Monday
    assert total_return_index(closes, div).tolist() == pytest.approx([1.0, 1.01, 1.01])
    assert total_return_index(closes).tolist() == pytest.approx([1.0, 1.0, 1.0])
    first_day = pd.Series([1.0], index=[idx[0]])  # already in the base close
    assert total_return_index(closes, first_day).tolist() == pytest.approx([1.0, 1.0, 1.0])


def test_brl_conversion_and_rebasing():
    idx = pd.bdate_range("2026-03-02", periods=3)
    usd = pd.Series([1.0, 1.1, 1.21], index=idx)
    fx = pd.Series([5.0, 5.5], index=[idx[0] - pd.Timedelta(days=3), idx[2]])  # gaps carried forward
    brl = to_brl(usd, fx)
    assert brl.loc[idx].tolist() == pytest.approx([5.0, 5.5, 1.21 * 5.5])
    assert rebased(brl, idx[1], idx[2]).tolist()[-1] == pytest.approx((1.21 * 5.5) / 5.5 * 100 - 100)


def test_risk_free_is_aligned_as_of_the_return_dates():
    idx = pd.bdate_range("2026-01-05", periods=6)
    r = pd.Series([0.01, -0.02, 0.015, 0.0, 0.004, -0.003], index=idx)
    sparse = pd.Series([0.001, 0.002], index=[idx[0], idx[3]])  # rate known on two dates only
    dense = pd.Series([0.001, 0.001, 0.001, 0.002, 0.002, 0.002], index=idx)
    assert risk_stats(r, sparse) == pytest.approx(risk_stats(r, dense))


def test_parse_sgs_rows_and_error_payloads():
    rows = [
        {"data": "30/10/2026", "valor": "0.050788"},
        {"data": "03/11/2026", "valor": "0.050788"},
        {"data": "03/11/2026", "valor": "0.051000"},  # revised print wins
    ]
    s = parse_sgs(rows)
    assert list(s.index) == [pd.Timestamp("2026-10-30"), pd.Timestamp("2026-11-03")]
    assert s.iloc[-1] == pytest.approx(0.051)
    assert parse_sgs([]).empty
    with pytest.raises((ValueError, KeyError)):
        parse_sgs({"error": "Requisição inválida"})  # type: ignore[arg-type]


def test_cdi_accrues_overnight_to_the_next_business_day():
    thu, fri, mon, tue = pd.to_datetime(["2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10"])
    level = accrual_index(pd.Series([1.0, 1.0, 1.0], index=[thu, fri, mon]))  # 1 % per day
    assert level.asof(thu) == pytest.approx(1.0)  # nothing earned before Thursday's close
    assert level.asof(fri) == pytest.approx(1.01)
    assert level.asof(mon) == pytest.approx(1.01**2)  # Friday's rate covers the weekend once
    assert level.asof(tue) == pytest.approx(1.01**3)  # Monday's rate known before it prints Tuesday
    sessions = pd.DatetimeIndex([thu, fri, mon, tue])
    assert period_returns(level, sessions).tolist() == pytest.approx([0.0, 0.01, 0.01, 0.01])


def test_cdi_level_on_a_holiday_excludes_the_next_overnight():
    dec29, dec30, jan2, jan5 = pd.to_datetime(["2025-12-29", "2025-12-30", "2026-01-02", "2026-01-05"])
    level = accrual_index(pd.Series(1.0, index=[dec29, dec30, jan2, jan5]))  # 1 % per business day
    jan1, sat = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-03")
    assert level.asof(jan1) == pytest.approx(level.asof(dec30))  # Dec 30 → Jan 2 not yet earned
    assert level.asof(sat) == pytest.approx(level.asof(jan2))  # weekend = Friday's close
    # YTD through Jan 5 earns Dec 30 → Jan 2 and Jan 2 → Jan 5, like a TWR based on Dec 30's close.
    assert change_since(level[level.index <= jan5], jan1) == pytest.approx((1.01**2 - 1) * 100)
    sessions = pd.DatetimeIndex([dec29, dec30, jan2, jan5])  # per-session risk-free unchanged
    assert period_returns(level, sessions).tolist() == pytest.approx([0.0, 0.01, 0.01, 0.01])


def test_change_since_uses_the_level_as_of_the_start():
    days = pd.date_range("2025-12-29", periods=8, freq="D")
    level = pd.Series(np.linspace(1.0, 1.07, 8), index=days)
    jan1 = pd.Timestamp("2026-01-01")
    assert change_since(level, jan1) == pytest.approx((1.07 / level[jan1] - 1) * 100)
    assert change_since(level, pd.Timestamp("2025-01-01")) == pytest.approx(7.0)  # opened later
    assert np.isnan(change_since(pd.Series(dtype=float), jan1))


def test_fold_other_keeps_the_palette_slot_count():
    v = pd.Series({f"S{i}": float(10 - i) for i in range(10)})
    out = fold_other(v, 8)
    assert len(out) == 8 and out.index[-1] == "Other"
    assert out["Other"] == pytest.approx(3.0 + 2.0 + 1.0)
    assert out.sum() == pytest.approx(v.sum())
    assert fold_other(v.iloc[:3], 8).index.tolist() == ["S0", "S1", "S2"]


def test_spread_labels_keeps_a_minimum_gap():
    placed = spread_labels({"Portfolio": 28.0, "SPY": 12.4, "CDI": 12.1}, min_gap=1.5)
    assert placed["CDI"] == pytest.approx(12.1)
    assert placed["SPY"] == pytest.approx(13.6)  # nudged above CDI
    assert placed["Portfolio"] == pytest.approx(28.0)  # far enough already


def test_empty_time_series_still_compare_with_dates():
    # A failed feed must not crash date clipping downstream (RangeIndex <= Timestamp raises).
    for empty in (parse_sgs([]), accrual_index(pd.Series(dtype=float)), to_brl(pd.Series(dtype=float), pd.Series(dtype=float))):
        assert isinstance(empty.index, pd.DatetimeIndex)
        assert empty[empty.index <= pd.Timestamp("2026-01-01")].empty
