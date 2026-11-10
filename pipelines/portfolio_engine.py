"""
portfolio_engine.py
───────────────────
Pure, vectorized portfolio accounting. No I/O — callers load the ledger and
persist the result.

The engine rebuilds the ENTIRE history from the ledger on every run. There is
no anchor row and no rolling window, so late trades, dividend uploads and
ledger corrections propagate deterministically into every metric.

Accounting conventions (tax and investor reporting depend on these):
  • Trade-date accounting. A BUY costs ``total + fee``; a SELL returns
    ``total - fee``.
  • Valuation uses RAW closes as printed on each B3 session — never
    dividend-adjusted prices. Dividends / JCP / fractions enter as cash on
    their payment date, so income is counted exactly once.
  • Corporate actions change share counts on the ex-date:
    ``new = floor(old * ratio)``. B3 auctions the fractions and pays cash,
    which arrives as a FRAÇÕES row in ``dividends``.
  • Cash: sale proceeds and income accumulate as uninvested cash and fund
    later buys. A buy that exceeds available cash is funded by an implied
    external contribution; cumulative contributions = ``total_invested``.
    Withdrawals are not modelled (the ledger records none).
  • Daily TWR with start-of-day flows:  r_t = V_t / (V_{t-1} + F_t) - 1,
    chain-linked  TWR_T = Π(1 + r_t) - 1.  Identical to a unitised NAV in
    which each contribution buys units at the prior close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

Severity = Literal["ERROR", "WARNING"]

STALE_PRICE_SESSIONS = 5  # held ticker whose last print is older than this → warn
LARGE_MOVE_THRESHOLD = 0.08  # |daily return| on a session worth a human look
PRICE_JUMP_THRESHOLD = 0.40  # raw close jump with no corporate action → likely missed split
TOTAL_MISMATCH_TOLERANCE = 0.05  # BRL; |total - quantity * price| beyond this is suspect
FRACTION_PAYMENT_WINDOW_DAYS = 150  # B3 pays auctioned fractions ~30-90 days after the ex-date
FRACTION_INCOME_TYPES = ("FRAÇÕES", "FRACOES")
# prices.source values that mark "B3 confirmed no trade this session". Their
# price is informational; the engine treats them as gaps (carry last print,
# corporate-action aware) so the staleness clock keeps running.
CARRY_SOURCES = ("b3_no_trade",)

PERFORMANCE_COLUMNS = [
    "date",
    "portfolio_value",
    "equity_value",
    "uninvested_cash",
    "total_invested",
    "external_inflow",
    "daily_return",
    "twr",
    "is_trading_day",
]


# ── Result types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Issue:
    """A validation finding. ERRORs block persistence; WARNINGs are reported."""

    severity: Severity
    code: str
    message: str
    date: pd.Timestamp | None = None


@dataclass
class EngineResult:
    """Everything the engine derived for one run."""

    performance: pd.DataFrame  # one row per calendar day, PERFORMANCE_COLUMNS
    positions: pd.DataFrame  # calendar days × tickers, share counts
    prices: pd.DataFrame  # calendar days × tickers, raw close used for valuation
    corporate_action_log: pd.DataFrame  # corporate actions that changed a holding
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    @property
    def ok(self) -> bool:
        return not self.errors


# ── Input normalisation ───────────────────────────────────────────────────────


def _as_day(values: pd.Series) -> pd.Series:
    """Coerce DB dates (date objects / strings / timestamps) to tz-naive midnight."""
    out = pd.to_datetime(values)
    if getattr(out.dt, "tz", None) is not None:
        out = out.dt.tz_localize(None)
    return out.dt.normalize().astype("datetime64[ns]")


def normalize_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Typed copy of the trades ledger with a signed quantity column.

    Args:
        trades: Rows from ``trades`` (date, side, ticker, quantity, price,
            total, fee).

    Returns:
        DataFrame sorted by date with ``signed_qty`` (+buy / -sell).
    """
    t = trades.copy()
    t["date"] = _as_day(t["date"])
    t["side"] = t["side"].astype(str).str.strip().str.upper()
    t["ticker"] = t["ticker"].astype(str).str.strip().str.upper()
    for col in ("quantity", "price", "total", "fee"):
        t[col] = pd.to_numeric(t[col], errors="coerce").astype(float)
    t["fee"] = t["fee"].fillna(0.0)
    t["signed_qty"] = np.where(t["side"] == "BUY", t["quantity"], -t["quantity"])
    # BUYs before SELLs inside a day: a same-day round trip never dips negative.
    t["_side_order"] = np.where(t["side"] == "BUY", 0, 1)
    return t.sort_values(["date", "_side_order"], kind="mergesort").drop(columns="_side_order")


def normalize_dividends(dividends: pd.DataFrame) -> pd.DataFrame:
    """Typed copy of the income ledger keyed on payment date."""
    d = dividends.copy()
    d["date"] = _as_day(d["dividend_date"])
    d["ticker"] = d["ticker"].astype(str).str.strip().str.upper()
    d["total"] = pd.to_numeric(d["total"], errors="coerce").astype(float)
    return d


def normalize_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Typed copy of raw closes; duplicates collapse to one row per (ticker, date).

    Keeps ``source`` when present ("" otherwise) so no-trade markers can be
    told apart from real prints.
    """
    p = prices[["date", "ticker", "price"]].copy()
    p["source"] = prices["source"].fillna("").astype(str) if "source" in prices.columns else ""
    p["date"] = _as_day(p["date"])
    p["ticker"] = p["ticker"].astype(str).str.strip().str.upper()
    p["price"] = pd.to_numeric(p["price"], errors="coerce").astype(float)
    return p.dropna(subset=["price"]).drop_duplicates(["ticker", "date"], keep="last")


def printed_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Normalized prices minus no-trade markers — only real session prints."""
    return prices[~prices["source"].isin(CARRY_SOURCES)]


def normalize_corporate_actions(actions: pd.DataFrame | None) -> pd.DataFrame:
    """Typed copy of corporate actions (ticker, ex_date, ratio)."""
    if actions is None or actions.empty:
        return pd.DataFrame(
            {
                "ticker": pd.Series(dtype=str),
                "ex_date": pd.Series(dtype="datetime64[ns]"),
                "ratio": pd.Series(dtype=float),
            }
        )
    a = actions.copy()
    a["ex_date"] = _as_day(a["ex_date"])
    a["ticker"] = a["ticker"].astype(str).str.strip().str.upper()
    a["ratio"] = pd.to_numeric(a["ratio"], errors="coerce").astype(float)
    return a.sort_values(["ex_date", "ticker"], kind="mergesort").reset_index(drop=True)


# ── Building blocks ───────────────────────────────────────────────────────────

# corporate_actions.ratio is NUMERIC(18,10): a repeating ratio is stored rounded
# (1/3 → 0.3333333333), so held × ratio can miss a whole number by held × 5e-11.
RATIO_STORAGE_PRECISION = 1e-10


def entitled_shares(held: float | np.ndarray, ratio: float | np.ndarray) -> np.ndarray:
    """Whole shares after a corporate action; B3 auctions the fractions.

    Args:
        held: Shares held at the end of the day before the ex-date.
        ratio: New shares per old share, as stored.

    Returns:
        floor(held × ratio), except that a product within the ratio's storage
        precision of a whole number is that number (300 × 0.3333333333 → 100).
    """
    exact = np.asarray(held, dtype=float) * np.asarray(ratio, dtype=float)
    nearest = np.round(exact)
    tolerance = np.abs(held) * RATIO_STORAGE_PRECISION + 1e-9  # + float noise
    return np.where(np.abs(exact - nearest) <= tolerance, nearest, np.floor(exact))


def build_positions(
    trades: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    days: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily share counts per ticker with corporate actions applied.

    Shares bought on an ex-date are not entitled (B3 "data com" is the prior
    session), so the entitlement is the position at the end of ex_date - 1.

    Args:
        trades: Output of :func:`normalize_trades`.
        corporate_actions: Output of :func:`normalize_corporate_actions`.
        days: Calendar index the positions are reported on.

    Returns:
        (positions, log) — positions is days × tickers; log lists every action
        that changed a non-zero holding, with the fraction B3 paid out in cash.
    """
    deltas = (
        trades.pivot_table(index="date", columns="ticker", values="signed_qty", aggfunc="sum")
        .reindex(days)
        .fillna(0.0)
    )
    log: list[dict] = []
    # Path-dependent by nature (each event floors the running position), but
    # there are only a handful of events — the per-day work stays vectorized.
    for ev in corporate_actions.itertuples(index=False):
        if ev.ticker not in deltas.columns or not (days[0] < ev.ex_date <= days[-1]):
            continue
        held = float(deltas.loc[: ev.ex_date - pd.Timedelta(days=1), ev.ticker].sum())
        if held <= 0:
            continue
        new = float(entitled_shares(held, ev.ratio))
        if new != held:
            deltas.loc[ev.ex_date, ev.ticker] += new - held
        log.append(
            {
                "ticker": ev.ticker,
                "ex_date": ev.ex_date,
                "ratio": ev.ratio,
                "shares_before": held,
                "shares_after": new,
                "fraction_paid_in_cash": round(max(held * ev.ratio - new, 0.0), 6),
            }
        )
    log_df = pd.DataFrame(
        log,
        columns=["ticker", "ex_date", "ratio", "shares_before", "shares_after", "fraction_paid_in_cash"],
    )
    return deltas.cumsum(), log_df


def corporate_action_factor(
    corporate_actions: pd.DataFrame,
    index: pd.DatetimeIndex,
    tickers: pd.Index,
) -> pd.DataFrame:
    """Cumulative share multiplier F(t) = Π ratio for every ex_date ≤ t.

    Only ratios F(s)/F(t) are ever used, so events before ``index`` start
    cancel out and are harmless.
    """
    log_steps = pd.DataFrame(0.0, index=index, columns=tickers)
    ca = corporate_actions[corporate_actions["ticker"].isin(tickers)]
    if not ca.empty:
        rows = index.searchsorted(ca["ex_date"].to_numpy())
        keep = rows < len(index)
        steps = (
            pd.DataFrame(
                {"row": rows[keep], "ticker": ca["ticker"].to_numpy()[keep], "lr": np.log(ca["ratio"].to_numpy()[keep])}
            )
            .groupby(["row", "ticker"])["lr"]
            .sum()
        )
        for (row, tk), lr in steps.items():
            log_steps.iat[row, log_steps.columns.get_loc(tk)] += lr
    return np.exp(log_steps.cumsum())


def build_price_matrix(
    prices: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    tickers: pd.Index,
    days: pd.DatetimeIndex,
    sessions: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Raw close per calendar day, its age in sessions, and a split-continuous series.

    Only bars dated on exchange sessions are used (providers emit
    carry-forward bars on holidays). A close carried across an ex-date is
    rescaled by the corporate-action ratio, so a split on a day without a
    print never values post-split shares at the pre-split price.

    Args:
        prices: Output of :func:`normalize_prices`.
        corporate_actions: Output of :func:`normalize_corporate_actions`.
        tickers: Columns to return.
        days: Calendar index to report on.
        sessions: Exchange sessions covering ``days``.

    Returns:
        (close, age, continuous) — close and age are days × tickers;
        ``age`` counts sessions since the close was printed (0 = printed on
        the latest session ≤ day). ``continuous`` is sessions × tickers in
        "per original share" terms: flat across correctly recorded corporate
        actions, so any large jump means the split ledger is incomplete.
    """
    on_session = prices[prices["date"].isin(sessions)]
    wide = (
        on_session.pivot_table(index="date", columns="ticker", values="price", aggfunc="last")
        .reindex(columns=tickers)
        .sort_index()
    )
    full_index = wide.index.union(days)
    factor = corporate_action_factor(corporate_actions, full_index, tickers)

    # Carry prices in per-original-share space, then convert back per day.
    continuous_full = (wide * factor.reindex(wide.index)).reindex(full_index).ffill()
    close = (continuous_full / factor).reindex(days)

    # Session ordinal of each print vs. the latest session on each day → age.
    sess_ord = pd.Series(np.arange(len(sessions), dtype=float), index=sessions)
    print_ord = (
        pd.DataFrame(
            np.where(wide.notna(), sess_ord.reindex(wide.index).to_numpy()[:, None], np.nan),
            index=wide.index,
            columns=wide.columns,
        )
        .reindex(full_index)
        .ffill()
        .reindex(days)
    )
    current_ord = sess_ord.reindex(sessions.union(days)).ffill().reindex(days)
    age = (-print_ord).add(current_ord, axis=0)
    continuous = continuous_full.reindex(wide.index)
    return close, age, continuous


def carry_prices(
    pairs: pd.DataFrame,
    closes: pd.DataFrame,
    corporate_actions: pd.DataFrame,
) -> pd.Series:
    """Reference price for sessions without a print: the last print, CA-adjusted.

    ``last_close × F(last_print_date) / F(day)`` — the price B3 itself uses as
    the reference after a split or bonus with no trades yet.

    Args:
        pairs: [ticker, date] needing a reference price.
        closes: Real prints [ticker, date, price] (no markers).
        corporate_actions: Output of :func:`normalize_corporate_actions`.

    Returns:
        Series aligned to ``pairs`` (NaN where the ticker never printed before).
    """
    if pairs.empty:
        return pd.Series(dtype=float)
    left = pairs[["ticker", "date"]].reset_index().sort_values("date")
    right = closes[["ticker", "date", "price"]].rename(columns={"date": "printed"}).sort_values("printed")
    hit = pd.merge_asof(
        left, right, left_on="date", right_on="printed", by="ticker", direction="backward", allow_exact_matches=False
    ).set_index("index")
    ratio = pd.Series(1.0, index=hit.index)
    for ev in corporate_actions.itertuples(index=False):  # handful of events
        between = (hit["ticker"] == ev.ticker) & (hit["printed"] < ev.ex_date) & (ev.ex_date <= hit["date"])
        ratio[between] *= ev.ratio
    return (hit["price"] / ratio).reindex(pairs.index)


def daily_cash_flows(
    trades: pd.DataFrame,
    dividends: pd.DataFrame,
    days: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Gross buys, net sale proceeds and income per calendar day."""
    is_buy = trades["side"] == "BUY"
    legs = pd.DataFrame(
        {
            "date": trades["date"],
            "buys": (trades["total"] + trades["fee"]).where(is_buy, 0.0),
            "sells": (trades["total"] - trades["fee"]).where(~is_buy, 0.0),
        }
    ).groupby("date")[["buys", "sells"]].sum()
    income = dividends.groupby("date")["total"].sum().rename("income")
    return legs.join(income, how="outer").reindex(days).fillna(0.0)


def implied_cash_and_contributions(net_flow: pd.Series) -> pd.DataFrame:
    """Vectorized cash account with implied external contributions.

    With C_t = Σ net flows, the cumulative contribution needed to keep cash
    non-negative is I_t = max(0, max_{s≤t} −C_s), and cash_t = C_t + I_t.
    This is the closed form of the day-by-day "top up any shortfall" walk.

    Args:
        net_flow: sells + income − buys per day.

    Returns:
        DataFrame with uninvested_cash, total_invested, external_inflow.
    """
    cumulative = net_flow.cumsum()
    invested = (-cumulative).cummax().clip(lower=0.0)
    inflow = invested.diff().fillna(invested.iloc[0])
    cash = (cumulative + invested).clip(lower=0.0)  # clip only absorbs fp dust
    return pd.DataFrame(
        {"uninvested_cash": cash, "total_invested": invested, "external_inflow": inflow}
    )


def time_weighted_returns(portfolio_value: pd.Series, inflow: pd.Series) -> pd.DataFrame:
    """Daily start-of-day-flow returns and their chain-linked TWR."""
    denominator = portfolio_value.shift(1, fill_value=0.0) + inflow
    daily = pd.Series(
        np.where(denominator > 0, portfolio_value / denominator.where(denominator > 0) - 1.0, 0.0),
        index=portfolio_value.index,
    )
    return pd.DataFrame({"daily_return": daily, "twr": (1.0 + daily).cumprod() - 1.0})


# ── Validation ────────────────────────────────────────────────────────────────


def _ledger_issues(
    trades: pd.DataFrame,
    dividends: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    first_day: pd.Timestamp,
) -> list[Issue]:
    issues: list[Issue] = []
    bad_side = trades[~trades["side"].isin(["BUY", "SELL"])]
    for r in bad_side.itertuples():
        issues.append(Issue("ERROR", "UNKNOWN_SIDE", f"{r.ticker} {r.date.date()}: side '{r.side}'", r.date))
    for r in trades[trades["fee"] < 0].itertuples():
        issues.append(
            Issue("WARNING", "NEGATIVE_FEE", f"{r.ticker} {r.side} {r.date.date()}: negative fee", r.date)
        )
    mismatch = (trades["total"] - trades["quantity"] * trades["price"]).abs() > TOTAL_MISMATCH_TOLERANCE
    for r in trades[mismatch].itertuples():
        issues.append(
            Issue(
                "WARNING",
                "TOTAL_MISMATCH",
                f"{r.ticker} {r.side} {r.date.date()}: quantity × price ≠ total",
                r.date,
            )
        )
    # Dated at the latest offending row, so the daily job alerts when a new one lands.
    early = dividends[dividends["date"] < first_day]
    if not early.empty:
        last = early["date"].max()
        issues.append(
            Issue("WARNING", "INCOME_BEFORE_FIRST_TRADE", f"{len(early)} income rows ignored (latest {last.date()})", last)
        )
    untraded = dividends[~dividends["ticker"].isin(trades["ticker"])]
    for tk, last in untraded.groupby("ticker")["date"].max().items():
        issues.append(
            Issue("WARNING", "INCOME_ON_UNTRADED_TICKER", f"{tk}: income but no trades (latest {last.date()})", last)
        )

    # Auctioned fractions only exist because of a bonus / split / reverse split.
    # Fractions with no recorded event = an event the provider missed, so share
    # counts may be wrong from its ex-date (Yahoo omits some B3 bonus issues).
    # A ratio-1 row is a rejected event (kept so the daily sync never re-adds it).
    if "type" in dividends.columns:
        kind = dividends["type"].astype(str).str.strip().str.upper()
        fractions = dividends[kind.isin(FRACTION_INCOME_TYPES)]
        events = corporate_actions[corporate_actions["ratio"] != 1.0]
        for r in fractions.itertuples():
            window = events[
                (events["ticker"] == r.ticker)
                & (events["ex_date"] <= r.date)
                & (events["ex_date"] >= r.date - pd.Timedelta(days=FRACTION_PAYMENT_WINDOW_DAYS))
            ]
            if window.empty:
                issues.append(
                    Issue(
                        "WARNING",
                        "FRACTIONS_WITHOUT_CORPORATE_ACTION",
                        f"{r.ticker} {r.date.date()}: fractions paid but no corporate action in the prior "
                        f"{FRACTION_PAYMENT_WINDOW_DAYS} days — record the bonus/split",
                        r.date,
                    )
                )
    return issues


def _valuation_issues(
    positions: pd.DataFrame,
    close: pd.DataFrame,
    age: pd.DataFrame,
    continuous: pd.DataFrame,
) -> list[Issue]:
    issues: list[Issue] = []
    held = positions > 0

    for tk in positions.columns[(positions < 0).any()]:
        d = positions.index[(positions[tk] < 0).to_numpy()][0]
        issues.append(
            Issue("ERROR", "NEGATIVE_POSITION", f"{tk} short from {d.date()} — missing buy or corporate action", d)
        )

    unpriced = held & close.isna()
    for tk in unpriced.columns[unpriced.any()]:
        d = unpriced.index[unpriced[tk].to_numpy()][0]
        issues.append(Issue("ERROR", "MISSING_PRICE", f"{tk} held from {d.date()} with no close yet", d))

    # One issue per contiguous stale streak, dated at its first day, so the
    # daily job can alert once when a streak starts instead of every day.
    stale = held & (age > STALE_PRICE_SESSIONS)
    starts = stale & ~stale.shift(1, fill_value=False)
    ends = stale & ~stale.shift(-1, fill_value=False)
    for tk in stale.columns[stale.any()]:
        for s, e in zip(stale.index[starts[tk].to_numpy()], stale.index[ends[tk].to_numpy()]):
            issues.append(
                Issue(
                    "WARNING",
                    "STALE_PRICE",
                    f"{tk}: valued at a close older than {STALE_PRICE_SESSIONS} sessions {s.date()} → {e.date()}",
                    s,
                )
            )

    # The split-continuous series is flat across recorded corporate actions.
    # A >40% jump there means a split is missing (or a bogus one is recorded),
    # i.e. share counts — and therefore equity — are wrong from that day.
    jumps = continuous.pct_change(fill_method=None).abs() > PRICE_JUMP_THRESHOLD
    for tk in jumps.columns[jumps.any()]:
        for d in jumps.index[jumps[tk].to_numpy()]:
            if d in positions.index and positions.at[d, tk] > 0:
                issues.append(
                    Issue(
                        "WARNING",
                        "PRICE_JUMP_NO_CORPORATE_ACTION",
                        f"{tk} {d.date()}: close moved >{PRICE_JUMP_THRESHOLD:.0%} with no matching corporate action",
                        d,
                    )
                )
    return issues


# ── Orchestration ─────────────────────────────────────────────────────────────


def run_engine(
    trades: pd.DataFrame,
    dividends: pd.DataFrame,
    prices: pd.DataFrame,
    corporate_actions: pd.DataFrame | None,
    sessions: pd.DatetimeIndex,
    end_date: pd.Timestamp,
) -> EngineResult:
    """Rebuild the full performance history.

    The series stops early if a session ≤ ``end_date`` has no prices loaded at
    all (price job has not run yet), so a fake 0% day is never published.

    Args:
        trades: Raw ``trades`` rows.
        dividends: Raw ``dividends`` rows (all income types).
        prices: Raw ``prices`` rows (raw closes).
        corporate_actions: Raw ``corporate_actions`` rows, or None.
        sessions: B3 sessions spanning the ledger.
        end_date: Last calendar day to report (normally yesterday, São Paulo).

    Returns:
        EngineResult; check ``.ok`` before persisting.
    """
    t = normalize_trades(trades)
    d = normalize_dividends(dividends)
    p_all = normalize_prices(prices)
    p = printed_prices(p_all)
    ca = normalize_corporate_actions(corporate_actions)
    sessions = pd.DatetimeIndex(sessions).normalize().astype("datetime64[ns]")
    end_date = pd.Timestamp(end_date).normalize()

    if t.empty:
        raise ValueError("trades ledger is empty — nothing to compute")
    first_day = t["date"].min()
    if end_date < first_day:
        raise ValueError(f"end date {end_date.date()} precedes the first trade {first_day.date()}")

    # Truncate at the first session on which something is held but no price at
    # all is loaded yet (price job has not run) — never publish a fake 0% day.
    loaded = set(p_all["date"])  # a no-trade marker still proves the session was processed
    full_days = pd.date_range(first_day, end_date, freq="D")
    held_any = build_positions(t, ca, full_days)[0].gt(0).any(axis=1)
    pending = [s for s in sessions if first_day <= s <= end_date and s not in loaded and held_any.get(s, False)]
    issues: list[Issue] = []
    if pending:
        cutoff = pending[0] - pd.Timedelta(days=1)
        issues.append(
            Issue(
                "WARNING",
                "TRUNCATED_END",
                f"no prices for session {pending[0].date()} — series ends {cutoff.date()} "
                f"instead of {end_date.date()}",
                pending[0],
            )
        )
        end_date = cutoff
    if end_date < first_day:
        raise ValueError(f"no complete sessions between {first_day.date()} and {end_date.date()}")

    days = pd.date_range(first_day, end_date, freq="D")
    in_range = sessions[(sessions >= first_day) & (sessions <= end_date)]

    positions, ca_log = build_positions(t, ca, days)
    close, age, continuous = build_price_matrix(p, ca, positions.columns, days, sessions)
    flows = daily_cash_flows(t, d[d["date"] >= first_day], days)

    equity = (positions * close).where(positions != 0, 0.0).sum(axis=1, min_count=0)
    cash = implied_cash_and_contributions(flows["sells"] + flows["income"] - flows["buys"])
    portfolio_value = equity + cash["uninvested_cash"]
    returns = time_weighted_returns(portfolio_value, cash["external_inflow"])

    perf = pd.DataFrame(
        {
            "date": days,
            "portfolio_value": portfolio_value.to_numpy(),
            "equity_value": equity.to_numpy(),
            "uninvested_cash": cash["uninvested_cash"].to_numpy(),
            "total_invested": cash["total_invested"].to_numpy(),
            "external_inflow": cash["external_inflow"].to_numpy(),
            "daily_return": returns["daily_return"].to_numpy(),
            "twr": returns["twr"].to_numpy(),
            "is_trading_day": days.isin(in_range),
        }
    )

    issues += _ledger_issues(t, d, ca, first_day)
    issues += _valuation_issues(positions, close, age, continuous)
    moves = perf[perf["is_trading_day"] & (perf["daily_return"].abs() > LARGE_MOVE_THRESHOLD)]
    for r in moves.itertuples():
        issues.append(
            Issue("WARNING", "LARGE_DAILY_MOVE", f"{r.date.date()}: daily return {r.daily_return:+.2%}", r.date)
        )

    return EngineResult(
        performance=perf[PERFORMANCE_COLUMNS],
        positions=positions,
        prices=close,
        corporate_action_log=ca_log,
        issues=issues,
    )


# ── Independent cross-checks (used by tests and the backfill report) ─────────


def average_cost_ledger(trades: pd.DataFrame, corporate_actions: pd.DataFrame | None) -> pd.DataFrame:
    """Brazilian average-cost (preço médio) ledger, event by event.

    Fees are capitalised on buys and deducted from sale proceeds. Splits and
    bonus shares change the share count; the cost basis is carried unchanged
    (bonus "custo atribuído" is NOT added — supply it separately for IRPF).

    This is an independent re-derivation of positions used to cross-check the
    vectorized engine, and the basis for realised-gain reporting.

    Args:
        trades: Raw ``trades`` rows.
        corporate_actions: Raw ``corporate_actions`` rows, or None.

    Returns:
        One row per event with running quantity, cost basis and realised P&L.
    """
    t = normalize_trades(trades)
    ca = normalize_corporate_actions(corporate_actions)
    events = pd.concat(
        [
            t.assign(kind=t["side"], ratio=np.nan)[["date", "ticker", "kind", "quantity", "total", "fee", "ratio"]],
            ca.assign(kind="CA", quantity=np.nan, total=np.nan, fee=np.nan)[
                ["ex_date", "ticker", "kind", "quantity", "total", "fee", "ratio"]
            ].rename(columns={"ex_date": "date"}),
        ],
        ignore_index=True,
    )
    # Corporate actions apply at the open of the ex-date, before that day's trades.
    order = {"CA": 0, "BUY": 1, "SELL": 2}
    events["_o"] = events["kind"].map(order)
    events = events.sort_values(["date", "_o"], kind="mergesort")

    state: dict[str, tuple[float, float]] = {}  # ticker → (qty, cost)
    rows: list[dict] = []
    for e in events.itertuples(index=False):
        qty, cost = state.get(e.ticker, (0.0, 0.0))
        realised = 0.0
        if e.kind == "CA":
            if qty <= 0:
                continue
            qty = float(entitled_shares(qty, e.ratio))
        elif e.kind == "BUY":
            qty += e.quantity
            cost += e.total + e.fee
        else:
            avg = cost / qty if qty > 0 else 0.0
            realised = (e.total - e.fee) - avg * e.quantity
            cost -= avg * e.quantity
            qty -= e.quantity
        state[e.ticker] = (qty, cost if qty > 0 else 0.0)
        rows.append(
            {
                "date": e.date,
                "ticker": e.ticker,
                "event": e.kind,
                "quantity_after": qty,
                "cost_basis_after": state[e.ticker][1],
                "realised_pnl": realised,
            }
        )
    return pd.DataFrame(rows)


def income_quantity_reconciliation(
    positions: pd.DataFrame,
    dividends: pd.DataFrame,
    lookback_days: int = FRACTION_PAYMENT_WINDOW_DAYS,
) -> pd.DataFrame:
    """Check each income row's share count against the ledger's positions.

    The ledger has payment dates but not record dates, so a row reconciles if
    its quantity equals the position on ANY day in the ``lookback_days`` before
    payment. A mismatch means a trade, corporate action or income row is wrong.
    Fraction rows are skipped (their quantity is a placeholder).

    Args:
        positions: ``EngineResult.positions``.
        dividends: Raw ``dividends`` rows.
        lookback_days: Window searched for a matching position.

    Returns:
        One row per income row: ticker, paid, type, quantity, status
        (OK / MISMATCH / NOT_HELD).
    """
    d = normalize_dividends(dividends)
    d = d[~d["type"].astype(str).str.strip().str.upper().isin(FRACTION_INCOME_TYPES)]
    out: list[dict] = []
    for r in d.itertuples():  # a few hundred rows; each needs its own window slice
        if r.ticker not in positions.columns:
            status = "NOT_HELD"
        else:
            window = positions.loc[r.date - pd.Timedelta(days=lookback_days) : r.date, r.ticker]
            status = "OK" if (window == r.quantity).any() else ("NOT_HELD" if (window <= 0).all() else "MISMATCH")
        out.append({"ticker": r.ticker, "paid": r.date, "type": r.type, "quantity": r.quantity, "status": status})
    return pd.DataFrame(out, columns=["ticker", "paid", "type", "quantity", "status"])


def xirr(dates: pd.Series, amounts: pd.Series, tol: float = 1e-10, max_iter: int = 200) -> float:
    """Money-weighted annual return (XIRR) via bisection on NPV.

    Args:
        dates: Cash-flow dates.
        amounts: Investor-perspective flows (contributions negative, terminal
            value positive).

    Returns:
        Annualised IRR as a decimal.
    """
    years = ((pd.to_datetime(dates) - pd.to_datetime(dates).min()).dt.days / 365.0).to_numpy()
    cf = np.asarray(amounts, dtype=float)

    def npv(rate: float) -> float:
        return float(np.sum(cf / (1.0 + rate) ** years))

    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        raise ValueError("XIRR not bracketed in [-99%, 1000%]")
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0
