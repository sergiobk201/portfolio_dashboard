"""Gap detection and the official-first fetch of the daily prices job."""

from __future__ import annotations

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


def _fake_b3(unavailable: set[pd.Timestamp], printed: dict[pd.Timestamp, dict[str, float]]):
    def fetch(day, timeout: int = 60) -> pd.DataFrame:
        day = pd.Timestamp(day)
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


def test_disagreements_ignore_no_trade_markers():
    stored = prices_frame([(S[0], "AAAA3", 10.0), (S[1], "AAAA3", 10.0, "b3_no_trade"), (S[2], "AAAA3", 10.0)])
    observed = prices_frame([(S[0], "AAAA3", 10.5), (S[1], "AAAA3", 12.0), (S[2], "AAAA3", 10.1)])
    out = pp.disagreements(observed, stored)
    assert out["date"].tolist() == [S[0]]
