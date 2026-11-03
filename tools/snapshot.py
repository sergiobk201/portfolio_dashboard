"""
snapshot.py
───────────
Read-only CSV snapshot of every public table, taken through the Management
API (no database password needed). Feeds ``rehearsal_up.sh``.

The output contains the FULL ledger: keep it out of git (``.repair/`` is
ignored) and delete it when the repair is done.

CLI:
    python tools/snapshot.py OUT_DIR
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from sb_query import run

PAGE_ROWS = 10_000
# table → ORDER BY key (stable paging). corporate_actions only exists after 001.
TABLES: dict[str, str] = {
    "assets": "ticker",
    "trades": "trade_id",
    "dividends": "dividend_id",
    "prices": "price_id",
    "benchmark": "bench_id",
    "portfolio_performance": "date",
    "corporate_actions": "ticker, ex_date",
}


def columns(table: str) -> list[str]:
    """Column names in table order (so empty tables still get a header)."""
    rows = run(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{table}' ORDER BY ordinal_position"
    )
    return [r["column_name"] for r in rows]


def dump(table: str, order: str, out_dir: Path) -> int:
    """Write one table to ``out_dir/<table>.csv``; returns the row count (-1 = absent)."""
    cols = columns(table)
    if not cols:
        return -1
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = run(f"SELECT * FROM public.{table} ORDER BY {order} LIMIT {PAGE_ROWS} OFFSET {offset}")
        rows.extend(chunk)
        if len(chunk) < PAGE_ROWS:
            break
        offset += PAGE_ROWS
    pd.DataFrame(rows, columns=cols).to_csv(out_dir / f"{table}.csv", index=False)
    return len(rows)


def main(argv: list[str]) -> int:
    """CLI entry point."""
    if len(argv) != 1:
        print(__doc__)
        return 2
    out_dir = Path(argv[0])
    out_dir.mkdir(parents=True, exist_ok=True)
    for table, order in TABLES.items():
        n = dump(table, order, out_dir)
        print(f"{table:24} {'absent' if n < 0 else f'{n:>7} rows'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
