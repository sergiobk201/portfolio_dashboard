"""
ledger_dedupe.py
────────────────
Idempotent loads for the hand-run ingest jobs (gmail_automation.py,
dividends_pipeline.py).

Both jobs mint random ids and the tables carry no natural unique key, so the
database cannot tell a re-run of the same nota or statement from new activity.
Incoming rows are matched to the stored ledger on what the broker printed,
as a MULTISET: a key stored n times absorbs the first n incoming copies.
Legitimately identical lines (several equal fills at one price on one nota)
therefore load once and are skipped on every re-run, instead of collapsing
into one row or doubling.

Pure pandas — no DB, PDF or Gmail dependencies — so it is unit-tested alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import pandas as pd

Normalizer = Callable[[pd.Series], pd.Series]

_OCCURRENCE = "_occurrence"


class LedgerUnavailableError(RuntimeError):
    """The stored ledger cannot be compared against (its database read failed)."""


def as_day(values: pd.Series) -> pd.Series:
    """Calendar day from ``datetime.date``, Timestamp or ISO-string values."""
    return pd.to_datetime(values).dt.normalize().astype("datetime64[ns]")


def as_cents(values: pd.Series) -> pd.Series:
    """Integer cents from float or Decimal money, so equality is exact."""
    return (pd.to_numeric(values) * 100).round().astype("int64")


def as_count(values: pd.Series) -> pd.Series:
    """Integer share counts (int32 from a parser, int64 or Decimal from the DB)."""
    return pd.to_numeric(values).astype("int64")


def as_code(values: pd.Series) -> pd.Series:
    """Trimmed, upper-case, NFC-normalised codes (tickers, sides, income types)."""
    return values.astype("string").str.normalize("NFC").str.strip().str.upper()


# A nota line / statement line is identified by what the document prints.
# Deliberately NOT in the keys:
#   ids        random per run;
#   portfolio  assigned by the job (DB lookup or prompt), not printed;
#   fee        re-derived on every run — a formula fix must not re-insert rows;
#   price, per-share dividend
#              implied by quantity and total, and stored at a different
#              precision than the document prints.
TRADE_KEY: dict[str, Normalizer] = {
    "date": as_day,
    "side": as_code,
    "ticker": as_code,
    "quantity": as_count,
    "total": as_cents,
}
DIVIDEND_KEY: dict[str, Normalizer] = {
    "dividend_date": as_day,
    "ticker": as_code,
    "type": as_code,
    "quantity": as_count,
    "total": as_cents,
}


@dataclass(frozen=True)
class LoadPlan:
    """An incoming batch split against the stored ledger.

    Attributes:
        new: Incoming rows with no stored counterpart, in incoming order.
        skipped: Incoming rows the ledger already holds (re-run duplicates).
    """

    new: pd.DataFrame
    skipped: pd.DataFrame


def _occurrence_index(frame: pd.DataFrame, key: Mapping[str, Normalizer]) -> pd.MultiIndex:
    """Normalised key plus a running count per key value (0, 1, 2, …)."""
    normalized = pd.DataFrame({col: norm(frame[col]) for col, norm in key.items()}, index=frame.index)
    normalized[_OCCURRENCE] = normalized.groupby(list(key), dropna=False, sort=False).cumcount()
    return pd.MultiIndex.from_frame(normalized)


def plan_idempotent_load(
    incoming: pd.DataFrame,
    existing: pd.DataFrame,
    key: Mapping[str, Normalizer],
) -> LoadPlan:
    """Keep only the incoming rows the ledger does not hold yet.

    Args:
        incoming: Rows parsed from one nota or statement.
        existing: Stored ledger rows; needs at least the key columns.
        key: Column → normalizer applied identically to both frames.

    Returns:
        LoadPlan with the rows to insert and the rows skipped as duplicates.

    Raises:
        LedgerUnavailableError: ``existing`` lacks key columns. The jobs fall
            back to a column-less frame when their database read fails, and
            loading blind would duplicate the whole batch.
    """
    missing = [col for col in key if col not in existing.columns]
    if missing:
        raise LedgerUnavailableError(
            f"stored ledger lacks columns {missing} — the database read failed; refusing to load blind"
        )
    is_stored = _occurrence_index(incoming, key).isin(_occurrence_index(existing, key))
    return LoadPlan(new=incoming.loc[~is_stored].copy(), skipped=incoming.loc[is_stored].copy())
