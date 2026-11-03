"""Synthetic ledger / quote builders for tests. SYNTHETIC DATA ONLY — this repository is public."""

from __future__ import annotations

import io
import zipfile

import pandas as pd

SESSIONS = pd.bdate_range("2025-01-06", periods=30)  # Mon 2025-01-06 … (no holidays)


def trades_frame(rows: list[tuple]) -> pd.DataFrame:
    """(date, side, ticker, quantity, price[, fee]) → trades rows; total = qty × price."""
    out = []
    for r in rows:
        date, side, ticker, qty, price, *rest = r
        out.append(
            {
                "date": date,
                "side": side,
                "ticker": ticker,
                "quantity": qty,
                "price": price,
                "total": round(qty * price, 2),
                "fee": rest[0] if rest else 0.0,
                "asset": f"{ticker} ON",
            }
        )
    return pd.DataFrame(out)


def prices_frame(rows: list[tuple]) -> pd.DataFrame:
    """(date, ticker, price[, source]) → prices rows."""
    return pd.DataFrame(
        [{"date": r[0], "ticker": r[1], "price": r[2], "source": r[3] if len(r) > 3 else "b3_cotahist"} for r in rows]
    )


def dividends_frame(rows: list[tuple]) -> pd.DataFrame:
    """(payment date, ticker, total[, type, quantity]) → dividends rows."""
    return pd.DataFrame(
        [
            {
                "dividend_date": r[0],
                "ticker": r[1],
                "total": r[2],
                "type": r[3] if len(r) > 3 else "DIVIDENDO",
                "quantity": r[4] if len(r) > 4 else 0,
            }
            for r in rows
        ],
        columns=["dividend_date", "ticker", "total", "type", "quantity"],
    )


def actions_frame(rows: list[tuple]) -> pd.DataFrame:
    """(ticker, ex_date, ratio) → corporate_actions rows."""
    return pd.DataFrame(rows, columns=["ticker", "ex_date", "ratio"])


def flat_prices(ticker: str, price: float, days: pd.DatetimeIndex) -> list[tuple]:
    return [(d, ticker, price) for d in days]


def cotahist_line(
    day: str,
    ticker: str,
    close: float,
    trades: int = 10,
    market: str = "010",
    factor: int = 1,
    record: str = "01",
) -> str:
    """One 245-char COTAHIST record (B3 SeriesHistoricas layout)."""
    buf = [" "] * 245

    def put(start: int, text: str) -> None:
        buf[start : start + len(text)] = list(text)

    put(0, record)
    put(2, day.replace("-", ""))
    put(10, "02")
    put(12, ticker.ljust(12))
    put(24, market)
    put(108, f"{round(close * 100):013d}")
    put(147, f"{trades:05d}")
    put(210, f"{factor:07d}")
    return "".join(buf)


def cotahist_zip(lines: list[str], name: str = "COTAHIST_A2025.TXT") -> bytes:
    header = "00COTAHIST.2025BOVESPA 20251231".ljust(245)
    trailer = "99COTAHIST.2025BOVESPA 20251231".ljust(245)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, "\r\n".join([header, *lines, trailer]).encode("latin-1"))
    return buf.getvalue()
