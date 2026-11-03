"""Smoke tests: the public dashboard renders on synthetic data — no DB, no network.

Skipped where the dashboard stack is not installed (the daily pipeline gate);
run in tests.yml, which installs requirements.txt.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")

import psycopg2  # noqa: E402
import requests  # noqa: E402
import streamlit as st  # noqa: E402
import yfinance as yf  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

try:
    from yfinance.exceptions import YFException
except ImportError:  # pragma: no cover - older yfinance
    YFException = RuntimeError  # type: ignore[misc,assignment]

APP = str(Path(__file__).resolve().parents[1] / "public_app.py")
DAYS = pd.date_range("2025-12-01", "2026-02-27", freq="D")
SESSIONS = DAYS[DAYS.dayofweek < 5]
SPLIT = pd.Timestamp("2026-01-15")


def _tables() -> dict[str, pd.DataFrame]:
    """Two segments, a 1:2 split, one JCP and one fraction payment."""
    r = np.where(DAYS.dayofweek < 5, 0.002 * np.sin(np.arange(len(DAYS))), 0.0)
    perf = pd.DataFrame(
        {"date": DAYS.date, "daily_return": r, "twr": np.cumprod(1 + r) - 1, "is_trading_day": DAYS.dayofweek < 5}
    )
    drift = 1 + 0.001 * np.arange(len(SESSIONS))
    prices = pd.concat(
        [pd.DataFrame({"date": SESSIONS.date, "ticker": tk, "price": p0 * drift}) for tk, p0 in [("AAAA3", 20.0), ("BBBB34", 50.0)]],
        ignore_index=True,
    )
    prices.loc[(prices["ticker"] == "AAAA3") & (pd.to_datetime(prices["date"]) >= SPLIT), "price"] /= 2
    return {
        "portfolio_performance": perf,
        "prices": prices,
        "trades": pd.DataFrame(
            {
                "date": [DAYS[0].date()] * 2,
                "side": ["BUY", "BUY"],
                "ticker": ["AAAA3", "BBBB34"],
                "quantity": [100, 10],
                "price": [20.0, 50.0],
                "total": [2000.0, 500.0],
                "fee": [0.0, 0.0],
                "portfolio": ["CORE", "GLOBAL"],
            }
        ),
        "dividends": pd.DataFrame(
            {
                "dividend_date": [date(2026, 1, 10), date(2026, 1, 20)],
                "ticker": ["AAAA3", "AAAA3"],
                "type": ["JUROS", "FRAÇÕES"],
                "dividend": [0.1, 1.0],
                "total": [10.0, 3.0],
                "quantity": [100, 3],
                "portfolio": ["CORE", "CORE"],
            }
        ),
        "corporate_actions": pd.DataFrame({"ticker": ["AAAA3"], "ex_date": [SPLIT.date()], "ratio": [2.0]}),
        "assets": pd.DataFrame({"ticker": ["AAAA3", "BBBB34"], "sector": ["Energy", None]}),
        "benchmark": pd.DataFrame({"date": SESSIONS.date, "price": 600.0 + np.arange(len(SESSIONS))}),
    }


class _Cursor:
    def __init__(self, tables: dict[str, pd.DataFrame]) -> None:
        self.tables, self.description, self._rows = tables, None, []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        table = re.search(r"FROM (\w+)", sql).group(1)
        cols = [c.strip() for c in re.search(r"SELECT (.+?) FROM", sql, re.S).group(1).split(",")]
        self.description = [SimpleNamespace(name=c) for c in cols]
        self._rows = list(self.tables[table][cols].itertuples(index=False, name=None))

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Conn:
    def __init__(self, tables: dict[str, pd.DataFrame]) -> None:
        self.tables, self.closed, self.session = tables, 0, {}

    def set_session(self, **kwargs: object) -> None:
        self.session = kwargs

    def cursor(self) -> _Cursor:
        return _Cursor(self.tables)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = 1


def _sgs_get(url: str, params: dict[str, str] | None = None, timeout: float | None = None) -> SimpleNamespace:
    code = int(re.search(r"sgs\.(\d+)/", url).group(1))
    days = pd.bdate_range(
        pd.to_datetime(params["dataInicial"], format="%d/%m/%Y"), pd.to_datetime(params["dataFinal"], format="%d/%m/%Y")
    )
    value = "0.05" if code == 12 else "5.00"  # CDI % per day / PTAX
    rows = [{"data": f"{d:%d/%m/%Y}", "valor": value} for d in days]
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: rows)


class _Ticker:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @property
    def dividends(self) -> pd.Series:
        idx = pd.DatetimeIndex(["2026-01-16 09:30"]).tz_localize("America/New_York")
        return pd.Series([1.5], index=idx)


def _decode(values: object) -> np.ndarray:
    """Plotly ≥ 6 ships arrays as base64 typed buffers."""
    if isinstance(values, dict):
        return np.frombuffer(base64.b64decode(values["bdata"]), dtype=values["dtype"])
    return np.asarray(values, dtype=float)


@pytest.fixture
def fake_backends(monkeypatch: pytest.MonkeyPatch) -> list[_Conn]:
    st.cache_data.clear()
    st.cache_resource.clear()
    conns: list[_Conn] = []

    def connect(**kwargs: object) -> _Conn:
        conns.append(_Conn(_tables()))
        return conns[-1]

    monkeypatch.setattr(psycopg2, "connect", connect)
    monkeypatch.setattr(requests, "get", _sgs_get)
    monkeypatch.setattr(yf, "Ticker", _Ticker)
    return conns


def _kpis(at: AppTest) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", m.value) for m in at.markdown if "class='card'" in m.value)


def test_dashboard_renders_with_every_feed(fake_backends: list[_Conn]):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.message for e in at.exception]
    assert not at.error
    assert fake_backends[0].session == {"readonly": True}  # the public app can never write
    kpis = _kpis(at)
    assert "SPY TR (BRL)" in kpis and "rf = CDI" in kpis
    card = at.dataframe[0].value.set_index("Portfolio")
    assert card.index.tolist() == ["CORE", "GLOBAL"] and card.at["CORE", "Weight %"] == "80.0 %"  # 1:2 split applied
    assert at.dataframe[2].value["Type"].tolist() == ["JUROS"]  # fraction cash is not a dividend

    charts = [json.loads(el.proto.spec) for el in at.get("plotly_chart")]
    lines = {tr["name"]: _decode(tr["y"]) for tr in charts[0]["data"]}
    assert set(lines) == {"Portfolio", "SPY TR (BRL)", "CDI"}
    assert all(abs(y[0]) < 1e-12 for y in lines.values())  # rebased to 0 at the window start
    pie = charts[1]["data"][0]
    assert _decode(pie["values"]).sum() == pytest.approx(100.0)  # weights only, never BRL


def test_dashboard_degrades_without_external_feeds(fake_backends: list[_Conn], monkeypatch: pytest.MonkeyPatch):
    def offline(*args: object, **kwargs: object) -> None:
        raise requests.ConnectionError("network down")

    class _RateLimited(_Ticker):
        @property
        def dividends(self) -> pd.Series:
            raise YFException("rate limited")

    monkeypatch.setattr(requests, "get", offline)
    monkeypatch.setattr(yf, "Ticker", _RateLimited)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.message for e in at.exception]
    kpis = _kpis(at)
    assert "SPY price (USD)" in kpis and "rf = 0 (CDI unavailable)" in kpis


def test_segment_filter_rerenders(fake_backends: list[_Conn]):
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.multiselect[0].select("📁 CORE").run()
    assert not at.exception, [e.message for e in at.exception]
    assert at.dataframe[1].value["Ticker"].tolist() == ["AAAA3"]  # Top Holdings follows the filter


def test_database_outage_shows_a_message_not_a_traceback(fake_backends: list[_Conn], monkeypatch: pytest.MonkeyPatch):
    def down(**kwargs: object) -> None:
        raise psycopg2.OperationalError("could not connect to server: secret-host.example")

    monkeypatch.setattr(psycopg2, "connect", down)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert "database unavailable" in at.error[0].value
    assert all("secret-host" not in c.value for c in at.caption)  # connection details never shown
