"""
market_data.py
──────────────
Exchange calendars and RAW daily closes.

"Raw" = the close printed on that session: not dividend-adjusted and not
split-adjusted. That is what the broker statement shows and what share counts
in the ledger are valued at.

Sources, in order of authority:
  1. B3 COTAHIST — the exchange's own historical quote files (official
     session closes, explicitly dated). A ticker that did not trade has no
     record, which is itself information: the session had no print.
  2. Yahoo — ``Close`` (auto_adjust=False) is split-adjusted, so it is
     multiplied back by every later split ratio. ``Adj Close`` (and
     ``auto_adjust=True``) is dividend-adjusted and must never be stored —
     that is what corrupted the 2024–2026 history. Zero-volume bars are
     carried quotes, not closes, and are dropped.
  3. BRAPI — recent closes only.

Error messages never include request URLs: they carry API keys/tokens and the
repository's Actions logs are public.
"""

from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Collection
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import requests
import yfinance as yf

try:  # yfinance ≥ 0.2.50 exposes a typed exception hierarchy
    from yfinance.exceptions import YFException
except ImportError:  # pragma: no cover - older yfinance
    YFException = RuntimeError  # type: ignore[misc,assignment]

B3_TZ = ZoneInfo("America/Sao_Paulo")
NY_TZ = ZoneInfo("America/New_York")
B3_CALENDAR = "BVMF"
NYSE_CALENDAR = "XNYS"

Market = Literal["B3", "US"]

FETCH_ERRORS = (
    requests.RequestException,
    zipfile.BadZipFile,
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    YFException,
)

B3_COTAHIST_URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/{name}.ZIP"
B3_MIN_DAILY_RECORDS = 300  # a real session file has ~1,000+ cash-market tickers
B3_TIMEOUT = (10.0, 30.0)  # (connect, read) seconds; the read timeout applies per chunk
B3_FILE_DEADLINE_S = 120.0  # a ~0.5 MB daily file normally arrives in seconds

# 1:1 ticker renames: the ledger books positions under the CURRENT code, but B3
# printed the old code before the switch date. {current: [(old, first day of current)]}
B3_TICKER_RENAMES: dict[str, list[tuple[str, str]]] = {
    "AXIA3": [("ELET3", "2025-11-10")],  # Eletrobras → Axia Energia
    "EMBJ3": [("EMBR3", "2025-11-03")],  # Embraer
    "MBRF3": [("MRFG3", "2025-09-23")],  # Marfrig → MBRF Global Foods (Marfrig holders 1:1)
}


# ── Calendars ─────────────────────────────────────────────────────────────────


def sessions(calendar: str, start: date | pd.Timestamp, end: date | pd.Timestamp) -> pd.DatetimeIndex:
    """Exchange sessions in [start, end] as tz-naive midnights."""
    cal = xcals.get_calendar(calendar)
    lo = max(pd.Timestamp(start), cal.first_session.tz_localize(None) if cal.first_session.tz else cal.first_session)
    hi = min(pd.Timestamp(end), cal.last_session.tz_localize(None) if cal.last_session.tz else cal.last_session)
    if lo > hi:
        return pd.DatetimeIndex([])
    out = cal.sessions_in_range(lo, hi)
    out = out.tz_localize(None) if out.tz is not None else out
    return pd.DatetimeIndex(out).normalize().astype("datetime64[ns]")


def last_completed_session(calendar: str, now: datetime) -> pd.Timestamp:
    """Latest session strictly before 'today' in the exchange's own timezone.

    Excluding today keeps the rule simple and deterministic no matter how late
    GitHub starts the scheduled job: a session's close is only ever stored
    after that calendar day is over.
    """
    tz = B3_TZ if calendar == B3_CALENDAR else NY_TZ
    today = pd.Timestamp(now.astimezone(tz).date())
    past = sessions(calendar, today - pd.Timedelta(days=15), today - pd.Timedelta(days=1))
    if past.empty:
        raise RuntimeError(f"no {calendar} session in the 15 days before {today.date()}")
    return past[-1]


def yesterday(tz: ZoneInfo, now: datetime) -> pd.Timestamp:
    """Calendar 'yesterday' in the given timezone."""
    return pd.Timestamp(now.astimezone(tz).date()) - pd.Timedelta(days=1)


# ── B3 COTAHIST (official closes) ─────────────────────────────────────────────


def parse_cotahist(payload: bytes, tickers: Collection[str] | None = None) -> pd.DataFrame:
    """Official session closes from a COTAHIST zip (daily or annual file).

    Fixed-width layout (B3 "SeriesHistoricas_Layout"): record type [0:2] = 01,
    date [2:10], ticker [12:24], market type [24:27], last price [108:121]
    (2 implied decimals), trade count [147:152], quote factor [210:217].
    Only the standard-lot cash market (010) is read — that is the close B3
    publishes for shares, units, ETFs and BDRs.

    Args:
        payload: Raw bytes of the .ZIP.
        tickers: Keep only these tickers (None keeps all).

    Returns:
        DataFrame[date, ticker, price, trades].
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        text = "".join(zf.read(name).decode("latin-1") for name in zf.namelist())
    lines = pd.Series(text.splitlines())
    lines = lines[(lines.str[:2] == "01") & (lines.str[24:27] == "010")]
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(lines.str[2:10], format="%Y%m%d"),
            "ticker": lines.str[12:24].str.strip(),
            "price": lines.str[108:121].astype("int64") / 100.0 / lines.str[210:217].astype("int64").clip(lower=1),
            "trades": lines.str[147:152].astype("int64"),
        }
    )
    if tickers is not None:
        out = out[out["ticker"].isin(set(tickers))]
    out["date"] = out["date"].astype("datetime64[ns]")
    # Annual files are not date-ordered within a ticker; callers rely on order.
    out = out.sort_values(["ticker", "date"], kind="mergesort")
    return out.drop_duplicates(["ticker", "date"], keep="last").reset_index(drop=True)


def b3_codes(tickers: Collection[str]) -> set[str]:
    """Ledger tickers plus every former B3 code they traded under."""
    codes = set(tickers)
    for current in tickers:
        codes |= {old for old, _ in B3_TICKER_RENAMES.get(current, [])}
    return codes


def apply_renames(closes: pd.DataFrame) -> pd.DataFrame:
    """Relabel pre-switch prints of renamed tickers with the current code.

    Prints of an old code on/after its switch date (should not exist) and
    old codes with no current mapping are dropped from the relabelled rows.
    """
    out = closes.copy()
    for current, history in B3_TICKER_RENAMES.items():
        for old, switch in history:
            is_old = out["ticker"] == old
            keep = is_old & (out["date"] < pd.Timestamp(switch))
            out.loc[keep, "ticker"] = current
            out = out[~(is_old & ~keep)]
    return out.drop_duplicates(["ticker", "date"], keep="last").reset_index(drop=True)


def _download(url: str, timeout: float | tuple[float, float], deadline_s: float) -> bytes:
    """GET ``url`` into memory, abandoned once ``deadline_s`` seconds have passed.

    requests' read timeout only bounds the wait for each chunk, so a host that
    trickles bytes would never time out on its own.

    Raises:
        requests.RequestException: HTTP error, timeout or an overdue download
            (the message never carries the URL).
    """
    start = time.monotonic()
    with requests.get(url, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if time.monotonic() - start > deadline_s:
                raise requests.Timeout(f"download exceeded {deadline_s:.0f}s")
            chunks.append(chunk)
    return b"".join(chunks)


def b3_daily_closes(
    day: date | pd.Timestamp,
    timeout: float | tuple[float, float] = B3_TIMEOUT,
    deadline_s: float = B3_FILE_DEADLINE_S,
) -> pd.DataFrame:
    """Every official close for one session (B3 publishes ~3h after the close).

    Args:
        day: Session to download.
        timeout: requests timeout, (connect, read) seconds.
        deadline_s: Wall-clock limit for the whole download.

    Raises:
        One of FETCH_ERRORS: HTTP 404 before publication, a timed-out or
        truncated download, or a file too small to be a real session.
    """
    name = f"COTAHIST_D{pd.Timestamp(day):%d%m%Y}"
    closes = parse_cotahist(_download(B3_COTAHIST_URL.format(name=name), timeout, deadline_s))
    if len(closes) < B3_MIN_DAILY_RECORDS or not (closes["date"] == pd.Timestamp(day)).all():
        raise ValueError(f"B3 file for {pd.Timestamp(day).date()} looks incomplete ({len(closes)} records)")
    return closes


def b3_annual_closes(
    year: int,
    tickers: Collection[str] | None = None,
    cache_dir: Path | None = None,
    timeout: int = 900,
) -> pd.DataFrame:
    """Official closes for a whole year (≈80–90 MB download; for backfills).

    Args:
        year: Calendar year; the current year's file is updated by B3 daily.
        tickers: Keep only these tickers.
        cache_dir: Reuse/stash the raw ZIP here (keeps a backfill repeatable).
        timeout: Download timeout in seconds.
    """
    name = f"COTAHIST_A{year}"
    cached = cache_dir / f"{name}.ZIP" if cache_dir is not None else None
    if cached is not None and cached.exists():
        payload = cached.read_bytes()
    else:
        resp = requests.get(B3_COTAHIST_URL.format(name=name), timeout=timeout)
        resp.raise_for_status()
        payload = resp.content
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(payload)
    return parse_cotahist(payload, tickers)


# ── Yahoo (raw closes + corporate actions) ────────────────────────────────────


def yahoo_symbol(ticker: str, market: Market = "B3") -> str:
    """Yahoo symbol: B3 tickers (shares, units, BDRs) carry a .SA suffix."""
    if market == "US" or any(ch in ticker for ch in ".^-"):
        return ticker
    return f"{ticker}.SA"


def split_unadjust(frame: pd.DataFrame) -> pd.Series:
    """Raw close = split-adjusted Close × product of split ratios AFTER each bar.

    Args:
        frame: One ticker's yfinance history (auto_adjust=False, actions=True),
            sorted by date, covering every split after the earliest bar of
            interest (i.e. fetched through today).

    Returns:
        Raw closes aligned to ``frame``.
    """
    ratio = frame["Stock Splits"].replace(0.0, 1.0).fillna(1.0).to_numpy(dtype=float)
    later = np.cumprod(ratio[::-1])[::-1]  # Π ratio for bars ≥ i
    after = np.append(later[1:], 1.0)  # Π ratio for bars > i
    return frame["Close"] * after


def yahoo_raw_history(
    ticker: str,
    start: date | pd.Timestamp,
    market: Market = "B3",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Raw daily closes since ``start`` plus split/bonus events from Yahoo.

    History is always fetched through today so every later split is visible
    for the un-adjustment.

    Args:
        ticker: Ticker without exchange suffix.
        start: First date wanted.
        market: "B3" (adds .SA) or "US".

    Returns:
        (prices[date, price, volume], actions[ex_date, ratio]).

    Raises:
        One of FETCH_ERRORS on network / payload problems; ValueError if empty.
    """
    hist = yf.Ticker(yahoo_symbol(ticker, market)).history(
        start=pd.Timestamp(start).date(),
        end=(datetime.now(B3_TZ) + timedelta(days=1)).date(),
        auto_adjust=False,
        actions=True,
    )
    if hist is None or hist.empty or "Close" not in hist.columns:
        raise ValueError(f"Yahoo returned no history for {ticker}")
    hist = hist.copy()
    idx = pd.DatetimeIndex(hist.index)
    hist.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
    hist = hist[~hist.index.duplicated(keep="last")].sort_index()
    if "Stock Splits" not in hist.columns:
        hist["Stock Splits"] = 0.0
    prices = pd.DataFrame(
        {
            "date": hist.index.astype("datetime64[ns]"),
            "price": split_unadjust(hist).to_numpy(),
            "volume": hist.get("Volume", pd.Series(np.nan, index=hist.index)).to_numpy(),
        }
    ).dropna(subset=["price"])
    # Zero-volume bars repeat the previous quote; Yahoo emits them even when
    # B3 printed trades (e.g. AMBP3 2026-10-01), so they are never a close.
    prices = prices[~(prices["volume"] == 0)]
    actions = hist.loc[hist["Stock Splits"].fillna(0) > 0, ["Stock Splits"]].reset_index()
    actions.columns = ["ex_date", "ratio"]
    return prices, actions


# ── BRAPI (primary feed for recent B3 closes) ────────────────────────────────


def brapi_history(ticker: str, token: str, range_: str = "1mo") -> pd.DataFrame:
    """Recent daily closes from BRAPI (raw, as printed).

    Bar timestamps are converted with their UTC date, which matched Yahoo's
    session dates on 99.7% of 6,049 overlapping bars (2026-03 → 2026-10).

    Args:
        ticker: B3 ticker.
        token: BRAPI API token.
        range_: BRAPI range parameter ("5d", "1mo", "3mo").

    Returns:
        DataFrame[date, price].

    Raises:
        One of FETCH_ERRORS on network / payload problems.
    """
    resp = requests.get(
        f"https://brapi.dev/api/quote/{ticker}",
        params={"range": range_, "interval": "1d", "fundamental": "false", "token": token},
        timeout=15,
    )
    resp.raise_for_status()
    history = resp.json()["results"][0].get("historicalDataPrice") or []
    bars = pd.DataFrame(history)
    if bars.empty or "close" not in bars.columns:
        raise ValueError(f"BRAPI returned no bars for {ticker}")
    bars = bars.dropna(subset=["close"])
    if "volume" in bars.columns:
        bars = bars[~(bars["volume"].fillna(0) == 0)]  # carried quote, not a close
    return pd.DataFrame(
        {
            "date": pd.to_datetime(bars["date"], unit="s").dt.normalize().astype("datetime64[ns]"),
            "price": bars["close"].astype(float),
        }
    ).drop_duplicates("date", keep="last")


# ── FMP (primary feed for SPY) ───────────────────────────────────────────────


def fmp_history(symbol: str, api_key: str, start: date, end: date) -> pd.DataFrame:
    """Daily closes from FMP's end-of-day endpoint, explicitly dated.

    Replaces the old ``quote.previousClose`` read, which labelled the wrong
    session whenever the job ran on a Friday/Saturday or after a holiday.

    Raises:
        One of FETCH_ERRORS on network / payload problems.
    """
    resp = requests.get(
        "https://financialmodelingprep.com/stable/historical-price-eod/full",
        params={"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "apikey": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("historical", payload) if isinstance(payload, dict) else payload
    bars = pd.DataFrame(rows)
    if bars.empty or "date" not in bars.columns:
        raise ValueError(f"FMP returned no bars for {symbol}")
    close_col = "close" if "close" in bars.columns else "price"
    return pd.DataFrame(
        {
            "date": pd.to_datetime(bars["date"]).dt.normalize().astype("datetime64[ns]"),
            "price": bars[close_col].astype(float),
        }
    ).drop_duplicates("date", keep="last")
