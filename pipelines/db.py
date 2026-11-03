"""
db.py
─────
Postgres access shared by every pipeline.

Credentials come from the environment variables the workflows already pass
(host, dbname, user, password, port). ``DB_SSLMODE`` overrides sslmode, which
only matters for a local test database (Supabase requires SSL).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal

import numpy as np
import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import connection as PgConnection
from psycopg2.extras import execute_values

LEDGER_QUERIES: dict[str, str] = {
    "trades": "SELECT trade_id, date, side, ticker, asset, quantity, price, total, fee, portfolio FROM trades",
    "dividends": (
        "SELECT dividend_id, dividend_date, ticker, type, dividend, total, quantity, portfolio FROM dividends"
    ),
    "prices": "SELECT date, ticker, price, source FROM prices",
    "corporate_actions": "SELECT ticker, ex_date, ratio, action_type FROM corporate_actions",
}


def connect_params() -> dict[str, str | int]:
    """Connection kwargs from the environment; fails fast if anything is missing."""
    missing = [k for k in ("host", "dbname", "user", "password") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing database environment variables: {', '.join(missing)}")
    return {
        "host": os.environ["host"],
        "dbname": os.environ["dbname"],
        "user": os.environ["user"],
        "password": os.environ["password"],
        "port": os.environ.get("port") or "5432",
        "sslmode": os.environ.get("DB_SSLMODE", "require"),
        "connect_timeout": 15,
    }


@contextmanager
def connect() -> Iterator[PgConnection]:
    """One transaction: commits on success, rolls back on any exception."""
    conn = psycopg2.connect(**connect_params())
    try:
        with conn:  # psycopg2: commit on clean exit, rollback on exception
            yield conn
    finally:
        conn.close()


def read_frame(conn: PgConnection, query: str, params: Sequence | None = None) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame with NUMERIC columns as float."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        cols = [c.name for c in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, Decimal)).any():
            df[col] = df[col].astype(float)
    return df


def load_ledger(conn: PgConnection) -> dict[str, pd.DataFrame]:
    """Every table the engine needs, keyed by table name."""
    return {name: read_frame(conn, q) for name, q in LEDGER_QUERIES.items()}


def _py(value: object) -> object:
    """Adapt numpy / pandas scalars to types psycopg2 understands."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if value is pd.NaT:
        return None
    return value


def upsert(
    conn: PgConnection,
    table: str,
    frame: pd.DataFrame,
    key: Sequence[str],
    *,
    update: bool,
    touch_column: str | None = None,
    page_size: int = 1000,
) -> int:
    """INSERT … ON CONFLICT for a DataFrame.

    Args:
        conn: Open connection (caller owns the transaction).
        table: Target table.
        frame: Rows to write; column names must match the table.
        key: Conflict target (must be a unique constraint).
        update: DO UPDATE the non-key columns if True, else DO NOTHING.
        touch_column: Timestamp column set to now() on update (e.g. updated_at).
        page_size: Rows per statement.

    Returns:
        Number of rows inserted or updated.
    """
    if frame.empty:
        return 0
    cols = list(frame.columns)
    non_key = [c for c in cols if c not in key]
    if update and non_key:
        assignments = [sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in non_key]
        if touch_column:
            assignments.append(sql.SQL("{c} = now()").format(c=sql.Identifier(touch_column)))
        action = sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(assignments)
    else:
        action = sql.SQL("DO NOTHING")
    statement = sql.SQL("INSERT INTO {t} ({cols}) VALUES %s ON CONFLICT ({key}) ").format(
        t=sql.Identifier(table),
        cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
        key=sql.SQL(", ").join(map(sql.Identifier, key)),
    ) + action

    rows = [tuple(_py(v) for v in r) for r in frame.itertuples(index=False, name=None)]
    written = 0
    with conn.cursor() as cur:
        query = statement.as_string(cur)
        for start in range(0, len(rows), page_size):  # rowcount only reflects one page
            execute_values(cur, query, rows[start : start + page_size], page_size=page_size)
            written += cur.rowcount
    return written
