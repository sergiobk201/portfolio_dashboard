"""Gap detection and the official-first fetch of the daily prices job."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, tzinfo

import pandas as pd
import pytest
import requests

import prices_pipeline as pp
from builders import SESSIONS as S
from builders import actions_frame, prices_frame, trades_frame


def ledger(trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {"trades": trades, "corporate_actions": actions_frame([])}


def test_required_pairs_cover_only_sessions_held_at_the_close():
    t = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0), (S[3], "SELL", "AAAA3", 100, 11.0)])
    req = pp.required_pairs(ledger(t), S[:6])
    assert req["date"].tolist() == list(S[:3]) and set(req["ticker"]) == {"AAAA3"}


def test_missing_and_upgradable_pairs():
    req = pp._pairs([("AAAA3", S[0]), ("AAAA3", S[1]), ("AAAA3", S[2]), ("AAAA3", S[3])])
    stored = prices_frame(
        [(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 10.0, "b3_no_trade"), (S[2], "AAAA3", 10.1, "yahoo_raw")]
    )
    assert pp.missing_pairs(req, stored)["date"].tolist() == [S[3]]  # a marker counts as present
    assert pp.upgradable_pairs(req, stored)["date"].tolist() == [S[2]]
    assert pp.missing_pairs(req, prices_frame([]).reindex(columns=["date", "ticker", "price", "source"])).shape[0] == 4


def test_anti_join_is_safe_on_empty_frames():
    key = ["ticker", "date"]
    some = pp._pairs([("AAAA3", S[0]), ("BBBB4", S[0])])
    empty = pp._pairs([])
    assert pp.anti_join(empty, some, key).empty
    assert pp.anti_join(some, empty, key).equals(some)
    assert pp.anti_join(some, some.iloc[:1], key)["ticker"].tolist() == ["BBBB4"]


@pytest.mark.parametrize(("ratio", "kind"), [(0.1, "REVERSE_SPLIT"), (2.0, "SPLIT"), (1.18, "BONUS")])
def test_classify_action(ratio: float, kind: str):
    assert pp.classify_action(ratio) == kind


def _fake_b3(
    unavailable: set[pd.Timestamp],
    printed: dict[pd.Timestamp, dict[str, float]],
    calls: list[tuple[pd.Timestamp, object]] | None = None,
):
    """b3_daily_closes stand-in; ``calls`` records (day, deadline_s) per download."""

    def fetch(day, **kw: object) -> pd.DataFrame:
        day = pd.Timestamp(day)
        if calls is not None:
            calls.append((day, kw.get("deadline_s")))
        if day in unavailable:
            raise requests.HTTPError("404")
        quotes = printed.get(day, {})
        return pd.DataFrame(
            {"date": [day] * len(quotes), "ticker": list(quotes), "price": list(quotes.values()), "trades": 1}
        )

    return fetch


def test_fetch_official_records_prints_no_trades_and_unavailable_files(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pp, "REQUEST_PAUSE_S", 0.0)
    monkeypatch.setattr(pp, "b3_daily_closes", _fake_b3({S[1]}, {S[0]: {"AAAA3": 10.0, "ZZZZ3": 1.0}}))
    pairs = pp._pairs([("AAAA3", S[0]), ("BBBB4", S[0]), ("AAAA3", S[1])])
    prints, no_trade, observed, notes = pp.fetch_official(pairs)
    assert prints[["ticker", "date", "price", "source"]].values.tolist() == [["AAAA3", S[0], 10.0, "b3_cotahist"]]
    assert no_trade.values.tolist() == [["BBBB4", S[0]]]  # absent from a valid file = no trade
    assert observed["ticker"].tolist() == ["AAAA3"]  # only held tickers are kept
    assert any("unavailable" in n for n in notes)  # S[1] is retried next run, not marked


def test_fetch_official_spends_the_budget_on_the_newest_sessions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pp, "REQUEST_PAUSE_S", 0.0)
    monkeypatch.setattr(pp, "b3_daily_closes", _fake_b3(set(), {d: {"AAAA3": 10.0} for d in S[:3]}))
    prints, _, _, notes = pp.fetch_official(pp._pairs([("AAAA3", d) for d in S[:3]]), max_files=2)
    assert sorted(prints["date"]) == list(S[1:3])
    assert any("budget" in n for n in notes)


def test_fetch_official_stops_after_consecutive_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pp, "REQUEST_PAUSE_S", 0.0)
    calls: list[tuple[pd.Timestamp, object]] = []
    monkeypatch.setattr(pp, "b3_daily_closes", _fake_b3(set(S[:5]), {}, calls))
    prints, no_trade, _, notes = pp.fetch_official(pp._pairs([("AAAA3", d) for d in S[:5]]))
    assert [d for d, _ in calls] == [S[4], S[3], S[2]]  # newest first, then B3 counts as down
    assert prints.empty and no_trade.empty  # nothing settled: all five go to the fallbacks
    assert any("3 consecutive failures — 2 session(s)" in n for n in notes)


def test_fetch_official_a_good_file_resets_the_failure_count(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pp, "REQUEST_PAUSE_S", 0.0)
    calls: list[tuple[pd.Timestamp, object]] = []
    good = {S[3]: {"AAAA3": 10.0}, S[1]: {"AAAA3": 10.0}}
    monkeypatch.setattr(pp, "b3_daily_closes", _fake_b3({S[4], S[2], S[0]}, good, calls))
    prints, _, _, notes = pp.fetch_official(pp._pairs([("AAAA3", d) for d in S[:5]]), max_failures=2)
    assert len(calls) == 5 and sorted(prints["date"]) == [S[1], S[3]]
    assert not any("stopped" in n for n in notes)


def test_fetch_official_caps_every_download_by_the_time_left(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pp, "REQUEST_PAUSE_S", 0.0)
    calls: list[tuple[pd.Timestamp, object]] = []
    monkeypatch.setattr(pp, "b3_daily_closes", _fake_b3(set(), {d: {"AAAA3": 10.0} for d in S[:5]}, calls))
    ticks = itertools.count(0.0, 100.0)  # each clock read is 100 s after the previous one
    prints, _, _, notes = pp.fetch_official(
        pp._pairs([("AAAA3", d) for d in S[:5]]), time_budget_s=350.0, clock=lambda: next(ticks)
    )
    assert calls == [(S[4], 120.0), (S[3], 120.0), (S[2], 50.0)]
    assert sorted(prints["date"]) == list(S[2:5])  # whatever arrived in time is kept
    assert any("350s — 2 session(s)" in n for n in notes)


def test_disagreements_ignore_no_trade_markers():
    stored = prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 10.0, "b3_no_trade"), (S[2], "AAAA3", 10.0)])
    observed = prices_frame([(S[0], "AAAA3", 10.5), (S[1], "AAAA3", 12.0), (S[2], "AAAA3", 10.1)])
    out = pp.disagreements(observed, stored)
    assert out["date"].tolist() == [S[0]]


# ── Corporate-action sync ─────────────────────────────────────────────────────

SINCE = pd.Timestamp("2025-01-01")


def _yahoo_cache(events: dict[str, list[tuple[str, float]]]) -> pp.YahooCache:
    """Per-ticker Yahoo split events, pre-cached so nothing is fetched."""
    return {
        tk: (
            SINCE,
            pd.DataFrame(columns=["date", "price"]),
            pd.DataFrame({"ex_date": pd.to_datetime([d for d, _ in evs]), "ratio": [r for _, r in evs]}),
        )
        for tk, evs in events.items()
    }


@pytest.mark.parametrize(("offset", "added"), [(-10, 0), (0, 0), (10, 0), (11, 1), (-11, 1)])
def test_ca_sync_takes_a_stored_row_within_the_window_as_the_same_event(offset: int, added: int):
    stored = actions_frame([("AAAA3", "2025-03-10", 1.0)])  # rejected by the operator: ratio = 1
    ex = f"{pd.Timestamp('2025-03-10') + pd.Timedelta(days=offset):%Y-%m-%d}"
    rows, _, skipped = pp.sync_corporate_actions(["AAAA3"], stored, _yahoo_cache({"AAAA3": [(ex, 2.0)]}), SINCE)
    assert len(rows) == added and skipped == []


def test_ca_sync_adds_one_row_per_event_within_a_run():
    stored = actions_frame([("BBBB4", "2025-03-10", 2.0)])  # another ticker's event never matches
    events = [("2025-03-14", 2.0), ("2025-03-10", 2.0), ("2025-06-02", 1.1), ("2024-12-02", 3.0)]
    rows, notes, _ = pp.sync_corporate_actions(["AAAA3"], stored, _yahoo_cache({"AAAA3": events}), SINCE)
    assert rows["ex_date"].tolist() == [pd.Timestamp("2025-03-10"), pd.Timestamp("2025-06-02")]
    assert rows["action_type"].tolist() == ["SPLIT", "BONUS"]
    assert any("ex 2025-03-14 taken as the one ex 2025-03-10" in n for n in notes)


def test_ca_sync_reports_the_tickers_it_could_not_check(monkeypatch: pytest.MonkeyPatch):
    def yahoo_down(*_: object, **__: object) -> None:
        raise ValueError("Yahoo returned no history")

    monkeypatch.setattr(pp, "yahoo_raw_history", yahoo_down)
    cache = _yahoo_cache({"AAAA3": []})
    rows, _, skipped = pp.sync_corporate_actions(["AAAA3", "BBBB4"], actions_frame([]), cache, SINCE)
    assert rows.empty and skipped == ["BBBB4"]
    _, notes, skipped = pp.sync_corporate_actions(["AAAA3", "BBBB4"], actions_frame([]), cache, SINCE, deadline=0.0)
    assert skipped == ["AAAA3", "BBBB4"] and any("fetch budget spent" in n for n in notes)


# ── main(): one daily run against an in-memory database ──────────────────────


class _FrozenNow:
    @staticmethod
    def now(tz: tzinfo | None = None) -> datetime:
        return datetime(2025, 1, 13, 12, 0, tzinfo=tz)  # Monday: target session S[4], Fri 2025-01-10


class _FakeDB:
    """The ledger main() reads, with db.upsert semantics applied to ``prices``."""

    def __init__(self, prices: pd.DataFrame) -> None:
        trades = trades_frame([(S[0], "BUY", "AAAA3", 100, 10.0)])
        self.ledger = {"trades": trades, "prices": prices, "corporate_actions": actions_frame([])}
        self.writes: list[tuple[str, pd.DataFrame, bool]] = []

    @contextmanager
    def connect(self) -> Iterator[_FakeDB]:
        yield self

    def load_ledger(self, conn: object) -> dict[str, pd.DataFrame]:
        return {name: frame.copy() for name, frame in self.ledger.items()}

    def upsert(self, conn: object, table: str, frame: pd.DataFrame, key: list[str], *, update: bool) -> int:
        self.writes.append((table, frame, update))
        if table != "prices" or frame.empty:
            return len(frame)
        rows, stored = frame[pp.PRICE_COLUMNS], self.ledger["prices"]
        kept, written = (pp.anti_join(stored, rows, key), rows) if update else (stored, pp.anti_join(rows, stored, key))
        self.ledger["prices"] = pd.concat([p for p in (kept, written) if not p.empty], ignore_index=True)
        return len(written)

    def written(self, update: bool) -> list[list[object]]:
        frames = [f for t, f, u in self.writes if t == "prices" and u == update and not f.empty]
        return pd.concat(frames)[pp.PRICE_COLUMNS].values.tolist() if frames else []


def _yahoo(bars: dict[str, dict[pd.Timestamp, float]], calls: list[str] | None = None):
    """yahoo_raw_history stand-in: closes per ticker, no split events; unknown tickers fail."""

    def history(ticker: str, start: object, market: str = "B3") -> tuple[pd.DataFrame, pd.DataFrame]:
        if calls is not None:
            calls.append(ticker)
        if ticker not in bars:
            raise ValueError(f"Yahoo returned no history for {ticker}")
        closes = bars[ticker]
        prices = pd.DataFrame({"date": pd.DatetimeIndex(list(closes)), "price": list(closes.values()), "volume": 1e3})
        return prices, pd.DataFrame({"ex_date": pd.DatetimeIndex([]), "ratio": pd.Series(dtype=float)})

    return history


def _run(monkeypatch: pytest.MonkeyPatch, fake: _FakeDB, b3, yahoo) -> tuple[int, list[str]]:
    sent: list[str] = []
    monkeypatch.setattr(pp, "load_dotenv", lambda: None)
    monkeypatch.delenv("BRAPI_KEY", raising=False)
    monkeypatch.setattr(pp, "REQUEST_PAUSE_S", 0.0)
    monkeypatch.setattr(pp, "datetime", _FrozenNow)
    monkeypatch.setattr(pp.db, "connect", fake.connect)
    monkeypatch.setattr(pp.db, "load_ledger", fake.load_ledger)
    monkeypatch.setattr(pp.db, "upsert", fake.upsert)
    monkeypatch.setattr(pp, "send_telegram", lambda message: sent.append(message) or True)
    monkeypatch.setattr(pp, "b3_daily_closes", b3)
    monkeypatch.setattr(pp, "yahoo_raw_history", yahoo)
    return pp.main(), sent


def test_main_turns_a_fallback_close_b3_shows_untraded_into_a_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    fake = _FakeDB(prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 10.1, "yahoo_raw"), (S[2], "AAAA3", 10.5, "yahoo_raw")]))
    calls: list[tuple[pd.Timestamp, object]] = []
    untraded = {"ZZZZ3": 1.0}  # a valid session file without AAAA3
    b3 = _fake_b3(set(), {S[1]: {"AAAA3": 10.2}, S[2]: untraded, S[3]: untraded, S[4]: {"AAAA3": 10.6}}, calls)
    code, sent = _run(monkeypatch, fake, b3, _yahoo({"AAAA3": {}}))
    assert code == 0 and sent == []
    assert [d for d, _ in calls] == [S[4], S[3], S[2], S[1]]
    # Markers carry the official S[1] close, never the demoted or the replaced fallback one.
    assert fake.written(update=True) == [["AAAA3", S[1], 10.2, "b3_cotahist"], ["AAAA3", S[2], 10.2, "b3_no_trade"]]
    assert fake.written(update=False) == [["AAAA3", S[4], 10.6, "b3_cotahist"], ["AAAA3", S[3], 10.2, "b3_no_trade"]]
    out = capsys.readouterr().out
    assert "fallback → no-trade: 1" in out and "AAAA3 2025-01-08: B3 file shows no trade" in out

    calls.clear()
    assert _run(monkeypatch, fake, b3, _yahoo({"AAAA3": {}}))[0] == 0
    assert calls == []  # settled: the S[2] file is not downloaded again


def test_main_alerts_when_the_target_session_comes_from_a_fallback(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeDB(prices_frame([(d, "AAAA3", 10.0) for d in S[:4]]))
    b3 = _fake_b3({S[4]}, {})
    code, sent = _run(monkeypatch, fake, b3, _yahoo({"AAAA3": {S[4]: 10.6}}))
    assert code == 0 and fake.written(update=False) == [["AAAA3", S[4], 10.6, "yahoo_raw"]]
    assert len(sent) == 1 and "B3 file for 2025-01-10 unavailable</b>: 1 close(s)" in sent[0]


def test_main_alerts_when_the_corporate_action_check_is_skipped(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeDB(prices_frame([(d, "AAAA3", 10.0) for d in S[:4]]))
    code, sent = _run(monkeypatch, fake, _fake_b3(set(), {S[4]: {"AAAA3": 10.6}}), _yahoo({}))
    assert code == 0 and len(sent) == 1 and "Corporate-action check skipped</b>: AAAA3" in sent[0]


def test_main_writes_what_it_fetched_when_the_fetch_budget_is_spent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pp, "FETCH_BUDGET_S", 0.0)
    fake = _FakeDB(prices_frame([(d, "AAAA3", 10.0) for d in S[:3]]))
    yahoo_calls: list[str] = []
    b3 = _fake_b3({S[3]}, {S[4]: {"AAAA3": 10.6}})
    code, sent = _run(monkeypatch, fake, b3, _yahoo({"AAAA3": {S[3]: 10.3}}, yahoo_calls))
    assert code == 0 and yahoo_calls == []  # no fallback, no corporate-action fetch
    assert fake.written(update=False) == [["AAAA3", S[4], 10.6, "b3_cotahist"]]
    assert "Corporate-action check skipped</b>: AAAA3" in sent[0]
