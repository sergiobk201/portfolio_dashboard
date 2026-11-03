"""
fingerprint.py
──────────────
Table fingerprints and the guarded single-transaction deploy builder.

A fingerprint is, per table, (row count, md5 of every row's text in a fixed
order) over VALUE columns only — serial ids and timestamps legitimately differ
between a replica and prod. Two databases with equal fingerprints hold the
same data.

The deploy commits only if prod's state BEFORE equals the snapshot the plan
was built from and its state AFTER equals the rehearsal; otherwise a guard
raises and the whole transaction rolls back.

Schemas:
    legacy   tables before sql/001 (no provenance, no corporate_actions)
    current  tables after sql/001

CLI:
    fingerprint.py query  {legacy|current}                  print the SELECT
    fingerprint.py prod   {legacy|current} OUT.csv          fingerprint prod (read-only)
    fingerprint.py build  --pre-schema S PRE.csv POST.csv OUT.sql FILE [FILE …]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

LEDGER: dict[str, tuple[str, str]] = {
    "trades": ("trade_id", "trade_id, date, side, ticker, quantity, price, total, fee, asset, portfolio"),
    "dividends": ("dividend_id", "dividend_id, dividend_date, ticker, dividend, quantity, total, type, portfolio"),
    "assets": ("ticker", "ticker, company_name, industry, sector"),
}
PERF = "date, portfolio_value, equity_value, uninvested_cash, total_invested, external_inflow, daily_return, twr"
SCHEMAS: dict[str, dict[str, tuple[str, str]]] = {
    "legacy": {
        "prices": ("price_id", "price_id, ticker, date, price"),
        "benchmark": ("bench_id", "bench_id, date, price"),
        "portfolio_performance": ("date", PERF),
        **LEDGER,
    },
    "current": {
        "prices": ("ticker, date", "ticker, date, price, source"),
        "benchmark": ("date", "date, price, source"),
        "corporate_actions": ("ticker, ex_date", "ticker, ex_date, ratio, action_type, source, notes"),
        "portfolio_performance": ("date", PERF + ", is_trading_day"),
        **LEDGER,
    },
}
FIELDS = ["t", "n", "h"]


def query(schema: str) -> str:
    """SELECT returning one (t, n, h) row per table of ``schema``."""
    parts = [
        f"SELECT '{t}' AS t, COUNT(*)::TEXT AS n, "
        f"MD5(COALESCE(STRING_AGG(ROW({cols})::TEXT, ',' ORDER BY {order}), '')) AS h FROM public.{t}"
        for t, (order, cols) in SCHEMAS[schema].items()
    ]
    return "\nUNION ALL\n".join(parts)


def guard(label: str, schema: str, expected: list[dict[str, str]]) -> str:
    """DO block that raises unless the live fingerprint equals ``expected``."""
    values = ",\n            ".join(f"('{r['t']}', '{r['n']}', '{r['h']}')" for r in expected)
    actual = query(schema).replace("\n", "\n        ")
    return f"""DO $guard$
DECLARE
    mismatched TEXT;
BEGIN
    WITH expected (t, n, h) AS (
        VALUES
            {values}
    ),
    actual AS (
        {actual}
    )
    SELECT STRING_AGG(e.t, ', ' ORDER BY e.t) INTO mismatched
    FROM expected AS e
    LEFT JOIN actual AS a USING (t)
    WHERE a.n IS DISTINCT FROM e.n OR a.h IS DISTINCT FROM e.h;
    IF mismatched IS NOT NULL THEN
        RAISE EXCEPTION '{label}-state guard failed for: % — nothing applied', mismatched;
    END IF;
END
$guard$;"""


def body(path: Path) -> str:
    """SQL file without its own transaction control (it runs inside ours)."""
    return re.sub(r"(?m)^(BEGIN|COMMIT);\s*$", "", path.read_text(encoding="utf-8")).strip()


def read_csv(path: Path) -> list[dict[str, str]]:
    """Fingerprint rows (t, n, h) from a CSV written by ``write_csv`` or ``psql --csv``."""
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict]) -> None:
    """LF line endings, so the file compares byte-for-byte with ``psql --csv``."""
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: str(r[k]) for k in FIELDS} for r in rows)


def build(pre_schema: str, pre: Path, post: Path, out: Path, files: list[Path]) -> None:
    """Write BEGIN; guard(pre); FILE…; guard(post); COMMIT; to ``out``."""
    sections = [
        "-- Guarded deploy: commits only if prod's state before AND after equals the rehearsal.",
        "BEGIN;",
        guard("pre", pre_schema, read_csv(pre)),
        *(f"-- ── {f.name} ──\n{body(f)}" for f in files),
        guard("post", "current", read_csv(post)),
        "COMMIT;",
    ]
    out.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"{out}: {out.stat().st_size / 1e6:.2f} MB")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the ``query`` / ``prod`` / ``build`` subcommands."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("query")
    q.add_argument("schema", choices=SCHEMAS)
    p = sub.add_parser("prod")
    p.add_argument("schema", choices=SCHEMAS)
    p.add_argument("out", type=Path)
    b = sub.add_parser("build")
    b.add_argument("--pre-schema", choices=SCHEMAS, required=True)
    b.add_argument("pre", type=Path)
    b.add_argument("post", type=Path)
    b.add_argument("out", type=Path)
    b.add_argument("files", type=Path, nargs="+")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    if args.cmd == "query":
        print(query(args.schema))
    elif args.cmd == "prod":
        from sb_query import run  # only this subcommand needs the CLI

        write_csv(args.out, run(query(args.schema)))
    else:
        build(args.pre_schema, args.pre, args.post, args.out, args.files)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
