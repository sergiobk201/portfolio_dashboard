"""Accounting invariants of portfolio_engine on synthetic ledgers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from builders import SESSIONS as S
from builders import actions_frame, dividends_frame, flat_prices, prices_frame, trades_frame
from portfolio_engine import (
    STALE_PRICE_SESSIONS,
    EngineResult,
    average_cost_ledger,
    carry_prices,
    entitled_shares,
    implied_cash_and_contributions,
    income_quantity_reconciliation,
    normalize_corporate_actions,
    run_engine,
    time_weighted_returns,
    xirr,
)

NO_INCOME = dividends_frame([])


def engine(trades, prices, end, dividends=NO_INCOME, actions=None, sessions=S) -> EngineResult:
    return run_engine(trades, dividends, prices, actions, sessions, end)


def codes(result: EngineResult) -> list[str]:
    return [i.code for i in result.issues]


def perf(result: EngineResult) -> pd.DataFrame:
    return result.performance.set_index("date")


# ── Returns and cash ─────────────────────────────────────────────────────────


def test_buy_and_hold_twr_is_the_price_return():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    p = prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 11.0), (S[2], "AAAA3", 12.1)])
    r = engine(t, p, S[2])
    f = perf(r)
    assert r.ok
    assert f.at[S[0], "daily_return"] == pytest.approx(0.0)
    assert f.at[S[0], "external_inflow"] == pytest.approx(1000.0)
    assert f["twr"].iloc[-1] == pytest.approx(0.21)
    assert f["portfolio_value"].iloc[-1] == pytest.approx(1210.0)
    assert f["total_invested"].iloc[-1] == pytest.approx(1000.0)


def test_fees_are_a_cost_on_the_trade_date():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0, 5.0)])
    r = engine(t, prices_frame([(S[0], "AAAA3", 10.0)]), S[0])
    last = r.performance.iloc[-1]
    assert last["total_invested"] == pytest.approx(1005.0)
    assert last["daily_return"] == pytest.approx(1000.0 / 1005.0 - 1.0)


def test_contribution_on_a_flat_day_is_a_zero_return():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0), (S[2], "BUY", "AAAA3", 100, 10.0)])
    r = engine(t, prices_frame(flat_prices("AAAA3", 10.0, S[:4])), S[3])
    f = perf(r)
    assert f.at[S[2], "external_inflow"] == pytest.approx(1000.0)
    assert f["total_invested"].iloc[-1] == pytest.approx(2000.0)
    assert np.allclose(f["daily_return"], 0.0, atol=1e-12)


def test_sale_proceeds_fund_the_next_buy_without_new_capital():
    t = trades_frame(
        [(S[0], "BUY", "AAAA3", 100, 10.0), (S[1], "SELL", "AAAA3", 100, 12.0), (S[2], "BUY", "BBBB4", 100, 12.0)]
    )
    p = prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 12.0), (S[2], "BBBB4", 12.0), (S[3], "BBBB4", 13.2)])
    r = engine(t, p, S[3])
    f = perf(r)
    assert r.ok
    assert f.loc[S[1]:, "external_inflow"].sum() == pytest.approx(0.0)
    assert f.at[S[1], "uninvested_cash"] == pytest.approx(1200.0)
    assert f.at[S[2], "uninvested_cash"] == pytest.approx(0.0)
    assert f["total_invested"].iloc[-1] == pytest.approx(1000.0)
    assert f["twr"].iloc[-1] == pytest.approx(1.2 * 1.1 - 1.0)


def test_income_is_counted_once_on_its_payment_date():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    d = dividends_frame([(S[2], "AAAA3", 50.0)])
    r = engine(t, prices_frame(flat_prices("AAAA3", 10.0, S[:4])), S[3], dividends=d)
    f = perf(r)
    assert f.at[S[2], "daily_return"] == pytest.approx(0.05)
    assert f["twr"].iloc[-1] == pytest.approx(0.05)
    assert f["total_invested"].iloc[-1] == pytest.approx(1000.0)


def test_same_day_round_trip_books_the_gain_once():
    t = trades_frame(
        [
            (S[0], "BUY", "AAAA3", 100, 10.0),
            (S[1], "SELL", "BBBB4", 100, 10.5),  # listed before its buy on purpose
            (S[1], "BUY", "BBBB4", 100, 10.0),
        ]
    )
    r = engine(t, prices_frame(flat_prices("AAAA3", 10.0, S[:2])), S[1])
    assert r.ok
    assert r.performance["daily_return"].iloc[-1] == pytest.approx(0.05)  # +50 on 1,000
    ledger = average_cost_ledger(t, None)
    assert ledger["realised_pnl"].sum() == pytest.approx(50.0)
    assert (ledger["quantity_after"] >= 0).all()


def test_closed_form_cash_matches_the_day_by_day_walk():
    flows = pd.Series(np.random.default_rng(7).normal(0.0, 1000.0, 500).round(2))
    closed = implied_cash_and_contributions(flows)
    cash = invested = 0.0
    cash_path, invested_path = [], []
    for f in flows:  # reference implementation: top up any shortfall
        cash += f
        if cash < 0:
            invested -= cash
            cash = 0.0
        cash_path.append(cash)
        invested_path.append(invested)
    np.testing.assert_allclose(closed["uninvested_cash"], cash_path, atol=1e-6)
    np.testing.assert_allclose(closed["total_invested"], invested_path, atol=1e-6)
    assert closed["external_inflow"].sum() == pytest.approx(invested)


def test_twr_equals_a_unitised_nav():
    """Start-of-day flows ≡ each contribution buying units at the prior NAV."""
    rng = np.random.default_rng(11)
    n = 250
    returns = rng.normal(0.0005, 0.012, n)
    flows = np.where(rng.random(n) < 0.1, rng.uniform(100, 5000, n), 0.0)
    flows[0] = 10_000.0
    value = np.zeros(n)
    units, nav = 0.0, 1.0
    for i in range(n):
        units += flows[i] / nav
        value[i] = ((value[i - 1] if i else 0.0) + flows[i]) * (1 + returns[i])
        nav = value[i] / units
    twr = time_weighted_returns(pd.Series(value), pd.Series(flows))["twr"]
    assert twr.iloc[-1] == pytest.approx(nav - 1.0, rel=1e-10)


def test_xirr_recovers_known_rates():
    one_year = pd.Series(pd.to_datetime(["2025-01-01", "2026-01-01"]))
    assert xirr(one_year, pd.Series([-1000.0, 1100.0])) == pytest.approx(0.10, abs=1e-9)

    dates = pd.Series(pd.to_datetime(["2025-01-01", "2025-07-02", "2026-01-01"]))
    amounts = pd.Series([-1000.0, -1000.0, 2200.0])
    rate = xirr(dates, amounts)
    years = (dates - dates.min()).dt.days / 365.0
    assert float((amounts / (1 + rate) ** years).sum()) == pytest.approx(0.0, abs=1e-6)


# ── Corporate actions ────────────────────────────────────────────────────────


def test_split_keeps_value_continuous():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    p = prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 10.0), (S[2], "AAAA3", 5.0), (S[3], "AAAA3", 5.0)])
    r = engine(t, p, S[3], actions=actions_frame([("AAAA3", S[2], 2.0)]))
    assert r.ok and "PRICE_JUMP_NO_CORPORATE_ACTION" not in codes(r)
    assert r.positions.at[S[1], "AAAA3"] == 100
    assert r.positions.at[S[2], "AAAA3"] == 200
    assert np.allclose(r.performance["daily_return"], 0.0, atol=1e-12)


def test_split_with_no_ex_date_print_carries_the_adjusted_close():
    """The no-trade marker proves the session was loaded; its price is ignored."""
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    p = prices_frame(
        [(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 10.0), (S[2], "AAAA3", 999.0, "b3_no_trade"), (S[3], "AAAA3", 5.5)]
    )
    r = engine(t, p, S[3], actions=actions_frame([("AAAA3", S[2], 2.0)]))
    f = perf(r)
    assert r.ok
    assert r.prices.at[S[2], "AAAA3"] == pytest.approx(5.0)
    assert f.at[S[2], "daily_return"] == pytest.approx(0.0)
    assert f.at[S[3], "daily_return"] == pytest.approx(0.10)


def test_missing_split_raises_a_price_jump_warning():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    r = engine(t, prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 5.0)]), S[1])
    assert "PRICE_JUMP_NO_CORPORATE_ACTION" in codes(r)


def test_bonus_floors_shares_and_logs_the_cash_fraction():
    t = trades_frame([(S[0], "BUY", "AAAA3", 105, 11.0)])
    p = prices_frame([(S[0], "AAAA3", 11.0), (S[1], "AAAA3", 10.0)])
    r = engine(t, p, S[1], actions=actions_frame([("AAAA3", S[1], 1.1)]))
    log = r.corporate_action_log.iloc[0]
    assert r.positions.at[S[1], "AAAA3"] == 115  # floor(105 × 1.1 = 115.5)
    assert (log["shares_before"], log["shares_after"]) == (105, 115)
    assert log["fraction_paid_in_cash"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("held", "stored_ratio", "expected"),
    [
        (300, 0.3333333333, 100),  # 3:1 reverse split, stored rounded down
        (300, 0.6666666667, 200),  # 2/3, stored rounded up
        (300, 1.3333333333, 400),  # 1-for-3 bonus
        (900, 1.1111111111, 1000),  # 1-for-9 bonus
        (3000, 0.0333333333, 100),  # 30:1 reverse split
        (105, 1.1, 115),  # genuine fraction: 115.5 → 115, the 0.5 is auctioned
        (10, 0.3333333333, 3),  # genuine fraction: 3.33 → 3
        (100, 5.0, 500),
    ],
)
def test_entitled_shares_absorbs_ratio_storage_rounding(held, stored_ratio, expected):
    assert float(entitled_shares(held, stored_ratio)) == expected


def test_repeating_ratio_reverse_split_keeps_every_share():
    """300 shares through a stored 1/3 reverse split are 100, so selling 100 later is not short."""
    t = trades_frame([(S[0], "BUY", "AAAA3", 300, 10.0), (S[2], "SELL", "AAAA3", 100, 31.0)])
    p = prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 30.0), (S[2], "AAAA3", 31.0)])
    ca = actions_frame([("AAAA3", S[1], 0.3333333333)])
    r = engine(t, p, S[2], actions=ca)
    assert r.ok and r.positions.at[S[1], "AAAA3"] == 100 and r.positions.at[S[2], "AAAA3"] == 0
    assert r.corporate_action_log.iloc[0]["fraction_paid_in_cash"] == 0
    assert average_cost_ledger(t, ca).iloc[1]["quantity_after"] == 100  # the IRPF ledger agrees


def test_income_warnings_are_dated_at_the_latest_offending_row():
    t = trades_frame([(S[2], "BUY", "AAAA3", 100, 10.0)])
    d = dividends_frame([(S[0], "AAAA3", 1.0), (S[3], "ZZZZ3", 2.0), (S[5], "ZZZZ3", 3.0)])
    r = engine(t, prices_frame(flat_prices("AAAA3", 10.0, S[:6])), S[5], dividends=d)
    dated = {i.code: (i.date, i.message) for i in r.warnings}
    assert dated["INCOME_BEFORE_FIRST_TRADE"][0] == S[0]
    assert dated["INCOME_ON_UNTRADED_TICKER"] == (S[5], f"ZZZZ3: income but no trades (latest {S[5].date()})")


def test_shares_bought_on_the_ex_date_are_not_entitled():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0), (S[1], "BUY", "AAAA3", 100, 5.0)])
    p = prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 5.0)])
    r = engine(t, p, S[1], actions=actions_frame([("AAAA3", S[1], 2.0)]))
    assert r.positions.at[S[1], "AAAA3"] == 300  # 100 × 2 + 100


def test_carry_prices_rescale_across_corporate_actions():
    closes = prices_frame([(S[0], "AAAA3", 10.0), (S[0], "BBBB4", 8.0)])
    pairs = pd.DataFrame({"ticker": ["AAAA3", "AAAA3", "BBBB4", "CCCC3"], "date": [S[1], S[3], S[3], S[3]]})
    ca = normalize_corporate_actions(actions_frame([("AAAA3", S[2], 2.0)]))
    out = carry_prices(pairs, closes, ca)
    assert out.iloc[:3].tolist() == pytest.approx([10.0, 5.0, 8.0])
    assert np.isnan(out.iloc[3])  # never printed


# ── Validation ───────────────────────────────────────────────────────────────


def test_bars_on_non_sessions_are_ignored():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    p = prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 10.0), (S[2], "AAAA3", 50.0), (S[3], "AAAA3", 10.0)])
    r = engine(t, p, S[3], sessions=S.delete(2))  # S[2] is a holiday
    f = perf(r)
    assert not f.at[S[2], "is_trading_day"]
    assert r.prices.at[S[2], "AAAA3"] == pytest.approx(10.0)
    assert np.allclose(f["daily_return"], 0.0, atol=1e-12)


def test_unpriced_holding_blocks_the_run():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0), (S[0], "BUY", "BBBB4", 10, 20.0)])
    r = engine(t, prices_frame([(S[0], "BBBB4", 20.0)]), S[0])
    assert not r.ok and [i.code for i in r.errors] == ["MISSING_PRICE"]


def test_selling_more_than_held_blocks_the_run():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0), (S[1], "SELL", "AAAA3", 150, 10.0)])
    r = engine(t, prices_frame(flat_prices("AAAA3", 10.0, S[:2])), S[1])
    assert "NEGATIVE_POSITION" in [i.code for i in r.errors]


def test_unloaded_session_truncates_instead_of_publishing_a_flat_day():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    r = engine(t, prices_frame(flat_prices("AAAA3", 10.0, S[:2])), S[4])
    assert r.ok and "TRUNCATED_END" in codes(r)
    assert r.performance["date"].iloc[-1] == S[2] - pd.Timedelta(days=1)


def test_end_before_first_trade_raises():
    t = trades_frame([(S[3], "BUY", "AAAA3", 100, 10.0)])
    with pytest.raises(ValueError, match="precedes the first trade"):
        engine(t, prices_frame([(S[3], "AAAA3", 10.0)]), S[1])


def test_no_trade_markers_keep_the_staleness_clock_running():
    n = STALE_PRICE_SESSIONS + 3
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    p = prices_frame(
        [(S[0], "AAAA3", 10.0)] + [(d, "AAAA3", 10.0, "b3_no_trade") for d in S[1:n]] + [(S[n], "AAAA3", 10.0)]
    )
    r = engine(t, p, S[n])
    stale = [i for i in r.issues if i.code == "STALE_PRICE"]
    assert "TRUNCATED_END" not in codes(r)
    assert len(stale) == 1 and stale[0].date == S[STALE_PRICE_SESSIONS + 1]  # one alert per streak


def test_fractions_without_a_recorded_event_are_flagged():
    t = trades_frame([(S[0], "BUY", "AAAA3", 105, 10.0)])
    p = prices_frame(flat_prices("AAAA3", 10.0, S[:6]))
    d = dividends_frame([(S[5], "AAAA3", 4.2, "FRAÇÕES")])
    assert "FRACTIONS_WITHOUT_CORPORATE_ACTION" in codes(engine(t, p, S[5], dividends=d))
    with_event = engine(t, p, S[5], dividends=d, actions=actions_frame([("AAAA3", S[1], 1.1)]))
    assert "FRACTIONS_WITHOUT_CORPORATE_ACTION" not in codes(with_event)
    rejected = engine(t, p, S[5], dividends=d, actions=actions_frame([("AAAA3", S[1], 1.0)]))
    assert "FRACTIONS_WITHOUT_CORPORATE_ACTION" in codes(rejected)  # ratio 1 = an event rejected by hand


def test_income_rows_reconcile_against_positions():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
    d = dividends_frame(
        [
            (S[4], "AAAA3", 50.0, "DIVIDENDO", 100),
            (S[5], "AAAA3", 9.0, "JCP", 90),
            (S[5], "ZZZZ3", 1.0, "DIVIDENDO", 10),
            (S[5], "AAAA3", 0.4, "FRAÇÕES", 0),  # skipped: placeholder quantity
        ]
    )
    r = engine(t, prices_frame(flat_prices("AAAA3", 10.0, S[:6])), S[5], dividends=d)
    rec = income_quantity_reconciliation(r.positions, d)
    assert rec["status"].tolist() == ["OK", "MISMATCH", "NOT_HELD"]


# ── Independent cross-check ──────────────────────────────────────────────────


def test_engine_reconciles_with_the_average_cost_ledger():
    """Positions agree, and gain = realised + unrealised + income exactly."""
    t = trades_frame(
        [
            (S[0], "BUY", "AAAA3", 100, 10.0, 1.0),
            (S[1], "BUY", "BBBB4", 50, 20.0, 1.0),
            (S[3], "SELL", "AAAA3", 40, 12.0, 0.5),
            (S[5], "BUY", "AAAA3", 30, 6.5, 0.5),  # after the 2:1 split
            (S[7], "SELL", "BBBB4", 50, 18.0, 0.5),
        ]
    )
    a = [10.0, 10.5, 11.0, 12.0, 6.2, 6.5, 6.6, 6.4, 6.8]
    b = [None, 20.0, 21.0, 19.0, 19.5, 20.0, 18.5, 18.0, None]
    p = prices_frame(
        [(S[i], "AAAA3", v) for i, v in enumerate(a)] + [(S[i], "BBBB4", v) for i, v in enumerate(b) if v]
    )
    ca = actions_frame([("AAAA3", S[4], 2.0)])
    income = 12.34
    r = engine(t, p, S[8], dividends=dividends_frame([(S[6], "AAAA3", income)]), actions=ca)
    assert r.ok

    events = average_cost_ledger(t, ca)
    final = events.groupby("ticker").tail(1).set_index("ticker")
    assert final["quantity_after"].to_dict() == {"AAAA3": 150.0, "BBBB4": 0.0}
    assert r.positions.iloc[-1].to_dict() == {"AAAA3": 150.0, "BBBB4": 0.0}

    held = final[final["quantity_after"] > 0]
    unrealised = float((held["quantity_after"] * r.prices.iloc[-1][held.index]).sum() - held["cost_basis_after"].sum())
    realised = float(events["realised_pnl"].sum())
    last = r.performance.iloc[-1]
    assert last["portfolio_value"] - last["total_invested"] == pytest.approx(realised + unrealised + income, abs=1e-9)
