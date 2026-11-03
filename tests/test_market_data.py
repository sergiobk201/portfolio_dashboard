"""COTAHIST parsing, ticker renames, split un-adjustment and calendars."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
import requests

import market_data
from builders import cotahist_line, cotahist_zip
from market_data import (
    B3_CALENDAR,
    B3_MIN_DAILY_RECORDS,
    NYSE_CALENDAR,
    apply_renames,
    b3_codes,
    last_completed_session,
    parse_cotahist,
    sessions,
    split_unadjust,
    yahoo_symbol,
)

TS = pd.Timestamp


def test_parse_cotahist_reads_only_the_standard_lot_cash_market():
    payload = cotahist_zip(
        [
            cotahist_line("2025-03-11", "AAAA3", 12.34, trades=812),
            cotahist_line("2025-03-10", "AAAA3", 12.00),  # annual files are not date-ordered
            cotahist_line("2025-03-10", "AAAA3F", 12.01, market="020"),  # odd lot
            cotahist_line("2025-03-10", "BBBB34", 61.50),
            cotahist_line("2025-03-10", "CCCC3", 4321.00, factor=1000),  # quoted per thousand
        ]
    )
    out = parse_cotahist(payload)
    assert out["ticker"].tolist() == ["AAAA3", "AAAA3", "BBBB34", "CCCC3"]
    assert out["date"].tolist() == [TS("2025-03-10"), TS("2025-03-11"), TS("2025-03-10"), TS("2025-03-10")]
    assert out["price"].tolist() == pytest.approx([12.00, 12.34, 61.50, 4.321])
    assert out.loc[1, "trades"] == 812
    assert str(out["date"].dtype) == "datetime64[ns]"


def test_parse_cotahist_filters_tickers_and_keeps_the_last_duplicate():
    payload = cotahist_zip(
        [
            cotahist_line("2025-03-10", "AAAA3", 12.00),
            cotahist_line("2025-03-10", "AAAA3", 12.05),
            cotahist_line("2025-03-10", "BBBB4", 7.00),
        ]
    )
    out = parse_cotahist(payload, {"AAAA3"})
    assert out[["ticker", "price"]].values.tolist() == [["AAAA3", 12.05]]


def test_renames_relabel_only_pre_switch_prints():
    closes = pd.DataFrame(
        {
            "ticker": ["ELET3", "ELET3", "AXIA3", "PETR4"],
            "date": pd.to_datetime(["2025-11-07", "2025-11-10", "2025-11-10", "2025-11-10"]),
            "price": [50.0, 99.0, 51.0, 30.0],
        }
    )
    out = apply_renames(closes).sort_values(["ticker", "date"]).reset_index(drop=True)
    assert out.values.tolist() == [
        ["AXIA3", TS("2025-11-07"), 50.0],
        ["AXIA3", TS("2025-11-10"), 51.0],
        ["PETR4", TS("2025-11-10"), 30.0],
    ]
    assert {"AXIA3", "ELET3", "PETR4"} <= b3_codes({"AXIA3", "PETR4"})


def test_split_unadjust_restores_printed_closes():
    frame = pd.DataFrame({"Close": [10.0, 10.0, 10.0, 10.0], "Stock Splits": [0.0, 0.0, 2.0, 0.0]})
    assert split_unadjust(frame).tolist() == [20.0, 20.0, 10.0, 10.0]


def test_yahoo_symbols():
    assert yahoo_symbol("PETR4") == "PETR4.SA"
    assert yahoo_symbol("AAPL34") == "AAPL34.SA"
    assert yahoo_symbol("SPY", market="US") == "SPY"
    assert yahoo_symbol("^BVSP") == "^BVSP"


def test_b3_calendar_holidays():
    nov = sessions(B3_CALENDAR, "2026-10-30", "2026-11-24")
    assert TS("2026-11-02") not in nov  # Finados
    assert TS("2026-11-20") not in nov  # Consciência Negra (national since 2024)
    assert TS("2026-11-03") in nov


@pytest.mark.parametrize(
    ("now_utc", "b3", "nyse"),
    [
        ("2026-11-03T12:00:00", "2026-10-30", "2026-11-02"),  # Tuesday after Finados
        ("2026-11-04T03:00:00", "2026-11-03", "2026-11-02"),  # BRT is past midnight, New York is not
    ],
)
def test_last_completed_session_uses_exchange_local_dates(now_utc: str, b3: str, nyse: str):
    now = datetime.fromisoformat(now_utc).replace(tzinfo=timezone.utc)
    assert last_completed_session(B3_CALENDAR, now) == TS(b3)
    assert last_completed_session(NYSE_CALENDAR, now) == TS(nyse)


class _Response:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content, self.status_code = content, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_b3_daily_file_must_look_like_a_full_session(monkeypatch: pytest.MonkeyPatch):
    tiny = cotahist_zip([cotahist_line("2025-03-10", "AAAA3", 12.0)])
    monkeypatch.setattr(market_data.requests, "get", lambda url, timeout: _Response(tiny))
    with pytest.raises(ValueError, match="incomplete"):
        market_data.b3_daily_closes(TS("2025-03-10"))

    full = cotahist_zip([cotahist_line("2025-03-10", f"T{i:04d}3", 1.0 + i) for i in range(B3_MIN_DAILY_RECORDS)])
    monkeypatch.setattr(market_data.requests, "get", lambda url, timeout: _Response(full))
    with pytest.raises(ValueError, match="incomplete"):  # file dated differently from the request
        market_data.b3_daily_closes(TS("2025-03-11"))
    assert len(market_data.b3_daily_closes(TS("2025-03-10"))) == B3_MIN_DAILY_RECORDS

    monkeypatch.setattr(market_data.requests, "get", lambda url, timeout: _Response(b"", 404))
    with pytest.raises(requests.HTTPError):  # not published yet
        market_data.b3_daily_closes(TS("2025-03-12"))
