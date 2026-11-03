"""
sb_query.py
───────────
Run SQL against the linked Supabase project through the Management API
(``supabase db query --linked``) and return the rows.

Used by the repair tooling to snapshot and fingerprint prod without a local
database password. The CLI must be linked once (see tools/README.md); its
working directory is taken from ``SUPABASE_WORKDIR``.

CLI:
    python tools/sb_query.py "SELECT 1 AS x"      # → CSV on stdout
    python tools/sb_query.py -f query.sql
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_WORKDIR = REPO / ".repair" / "supabase"


class SupabaseQueryError(RuntimeError):
    """The Management API rejected or failed the query (message included)."""


def workdir() -> Path:
    """Directory where ``supabase link`` was run (``SUPABASE_WORKDIR``)."""
    return Path(os.environ.get("SUPABASE_WORKDIR", DEFAULT_WORKDIR)).expanduser()


def run(sql: str, timeout: int = 600) -> list[dict]:
    """Execute SQL on prod via the Management API.

    The SQL travels in a temp file (``-f``): argv is capped at ~1 MB on macOS
    and deploy payloads are larger.

    Args:
        sql: One or more statements; rows of the last one are returned.
        timeout: Seconds before the CLI call is abandoned.

    Returns:
        Result rows as dicts (empty for statements without a result set).

    Raises:
        SupabaseQueryError: Non-JSON output, an API error or a failed query.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".sql", encoding="utf-8", delete=False) as fh:
        fh.write(sql)
        path = fh.name
    try:
        out = subprocess.run(
            ["supabase", "db", "query", "--linked", "--workdir", str(workdir()), "-f", path],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SupabaseQueryError(f"supabase CLI timed out after {timeout}s") from exc
    finally:
        Path(path).unlink(missing_ok=True)

    text = out.stdout
    start = text.find("{")
    if out.returncode != 0 or start < 0:
        raise SupabaseQueryError((out.stderr + text).strip()[-2000:])
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise SupabaseQueryError(f"unparseable CLI output: {exc}") from exc
    if "rows" not in payload:
        raise SupabaseQueryError(json.dumps(payload)[:2000])
    return payload["rows"]


def main(argv: list[str]) -> int:
    """CLI entry point: print the rows as CSV."""
    if not argv:
        print(__doc__)
        return 2
    sql = Path(argv[1]).read_text(encoding="utf-8") if argv[0] == "-f" else argv[0]
    rows = run(sql)
    if not rows:
        print("(0 rows)")
        return 0
    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
