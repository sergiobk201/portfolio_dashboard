"""Statement parsing and idempotent re-runs of the income loader.

The job runs end to end against an in-memory ledger wired in through
psycopg2.connect / pandas.read_sql_query / pandas.read_excel.
SYNTHETIC DATA ONLY — this repository is public.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
import psycopg2
import pytest

import dividends_pipeline as dp
from ledger_dedupe import LedgerUnavailableError

COLUMNS = ["dividend_id", "dividend_date", "ticker", "type", "dividend", "total", "quantity", "portfolio"]
CENT = Decimal("0.01")

A = ("2025-03-10", "DIVIDENDOS CLIENTE AAAA3 S/ 100", 12.34)
B = ("2025-03-10", "JUROS S/ CAPITAL BBBB4 S/ 7", 0.70)
C = ("2025-03-11", "FRAÇÕES DE AÇÕES CCCC3", 0.05)
D = ("2025-03-12", "RENDIMENTOS CLIENTE DDDD11 S/ 3", 0.36)
NOT_INCOME = ("2025-03-11", "TED RETIRADA CONTA", -100.00)
BLANK = None  # an empty sheet row


def statement_sheet(lines: list[tuple[str, str, float] | None], footer: bool = True) -> pd.DataFrame:
    """(movement date, description, amount) or BLANK → the sheet as read below its 13-row header."""
    rows = [
        [None] * 7 if line is BLANK else [None, pd.Timestamp(line[0]), pd.Timestamp(line[0]), line[1], None, line[2], 0.0]
        for line in lines
    ]
    if footer:
        rows += [[None] * 7, [None, None, None, "SALDO FINAL", None, None, 0.0]]
    return pd.DataFrame(rows)


class FakeLedger:
    """The two tables the job reads, plus the dividends insert, held in memory."""

    def __init__(self, portfolios: dict[str, str]) -> None:
        self.trades = pd.DataFrame({"ticker": list(portfolios), "portfolio": list(portfolios.values())})
        self.dividends = pd.DataFrame(columns=COLUMNS)
        self.fail_insert = False
        self.fail_dividends_read = False
        self.commits = self.rollbacks = 0

    def connect(self, **_: object) -> "FakeLedger":
        return self

    def cursor(self) -> "FakeLedger":
        return self

    def __enter__(self) -> "FakeLedger":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None

    def copy_from(self, file, table: str, sep: str, columns: list[str]) -> None:
        assert table == "dividends"
        if self.fail_insert:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        rows = pd.read_csv(file, sep=sep, header=None, names=columns, dtype=str)
        self.dividends = rows if self.dividends.empty else pd.concat([self.dividends, rows], ignore_index=True)

    def read_sql_query(self, sql: str, _connection: object) -> pd.DataFrame:
        if "from trades" in sql.lower():
            return self.trades.copy()
        if self.fail_dividends_read:
            raise pd.errors.DatabaseError("Execution failed on sql")
        stored = self.dividends.astype(object)  # psycopg2 types: DATE → date, NUMERIC → Decimal
        stored["dividend_date"] = [pd.Timestamp(v).date() for v in self.dividends["dividend_date"]]
        for col in ("dividend", "total"):  # NUMERIC(10, 2) rounds ties away from zero
            stored[col] = [Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP) for v in self.dividends[col]]
        stored["quantity"] = [int(v) for v in self.dividends["quantity"]]
        return stored


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> FakeLedger:
    fake = FakeLedger({"AAAA3": "ALPHA", "BBBB4": "BETA", "CCCC3": "ALPHA", "DDDD11": "BETA"})
    monkeypatch.setattr(dp.psycopg2, "connect", fake.connect)
    monkeypatch.setattr(dp.pd, "read_sql_query", fake.read_sql_query)

    def no_prompt(prompt: str = "") -> str:
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr("builtins.input", no_prompt)
    return fake


def run(monkeypatch: pytest.MonkeyPatch, lines: list[tuple[str, str, float]]) -> int:
    monkeypatch.setattr(dp.pd, "read_excel", lambda *_a, **_k: statement_sheet(lines))
    return dp.DividendsPipeline("statement.xlsx").run_pipeline()


def test_parse_statement_reads_income_lines_up_to_the_footer():
    out = dp.parse_statement(statement_sheet([A, NOT_INCOME, B, C]))
    assert out.columns.tolist() == COLUMNS
    assert out[["ticker", "type", "quantity"]].values.tolist() == [
        ["AAAA3", "DIVIDENDOS", 100],
        ["BBBB4", "JUROS", 7],
        ["CCCC3", "FRAÇÕES", 1],
    ]
    assert out["dividend"].tolist() == pytest.approx([0.12, 0.10, 0.05])
    assert out["dividend_id"].isna().all() and (out["portfolio"] == dp.unresolved_placeholder).all()


def test_parse_statement_without_a_blank_row_keeps_every_line():
    # The first-blank-row cut used argmax(), which is 0 when no row is blank.
    assert len(dp.parse_statement(statement_sheet([A, B, C], footer=False))) == 3


def test_rerun_of_the_same_statement_inserts_nothing(ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch):
    assert run(monkeypatch, [A, B, C]) == 3
    assert run(monkeypatch, [A, B, C]) == 0
    assert len(ledger.dividends) == 3 and ledger.commits == 1
    assert sorted(ledger.dividends["portfolio"]) == ["ALPHA", "ALPHA", "BETA"]


def test_overlapping_statement_loads_only_the_new_lines(ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch):
    assert run(monkeypatch, [A, B, C]) == 3
    assert run(monkeypatch, [B, C, D]) == 1
    assert sorted(ledger.dividends["ticker"]) == ["AAAA3", "BBBB4", "CCCC3", "DDDD11"]


def test_identical_lines_load_once_and_are_not_collapsed(ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch):
    assert run(monkeypatch, [A, B, B]) == 3  # two payments printed identically
    assert run(monkeypatch, [A, B, B]) == 0
    assert run(monkeypatch, [B, B, B]) == 1  # a third copy is new
    assert (ledger.dividends["ticker"] == "BBBB4").sum() == 3


def test_failed_ledger_read_refuses_to_load(ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch):
    ledger.fail_dividends_read = True
    with pytest.raises(LedgerUnavailableError):
        run(monkeypatch, [A])
    assert ledger.dividends.empty and ledger.commits == 0


def test_failed_insert_is_raised_not_reported_as_success(ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch):
    ledger.fail_insert = True
    with pytest.raises(psycopg2.OperationalError):
        run(monkeypatch, [A])
    assert ledger.dividends.empty and ledger.rollbacks == 1
    assert issubclass(psycopg2.OperationalError, dp.CLI_ERRORS)  # the CLI reports a failure, not success


def test_share_counts_with_a_thousands_dot_parse():
    out = dp.parse_statement(statement_sheet([("2025-03-10", "DIVIDENDOS CLIENTE AAAA3 S/ 1.500", 150.00)]))
    assert out[["ticker", "quantity", "dividend"]].values.tolist() == [["AAAA3", 1500, 0.10]]


@pytest.mark.parametrize("count", ["1,5", "1.50", "CLIENTE"])
def test_a_share_count_that_is_not_a_whole_number_fails(count: str):
    with pytest.raises(ValueError, match=f"share count '{count}' is not a whole number"):
        dp.parse_statement(statement_sheet([A, ("2025-03-10", f"DIVIDENDOS CLIENTE BBBB4 S/ {count}", 1.0)]))


def test_a_blank_row_mid_sheet_does_not_drop_the_lines_after_it():
    out = dp.parse_statement(statement_sheet([A, BLANK, NOT_INCOME, B, BLANK, C]))
    assert out["ticker"].tolist() == ["AAAA3", "BBBB4", "CCCC3"]


def test_types_match_in_any_unicode_form_and_singular_rendimento():
    decomposed = (C[0], unicodedata.normalize("NFD", C[1]), C[2])
    singular = ("2025-03-12", "RENDIMENTO CLIENTE DDDD11 S/ 3", 0.36)
    out = dp.parse_statement(statement_sheet([decomposed, singular]))
    assert out[["ticker", "type", "quantity"]].values.tolist() == [["CCCC3", "FRAÇÕES", 1], ["DDDD11", "RENDIMENTOS", 3]]


def test_a_half_cent_amount_is_stored_and_matched_at_the_same_cent(ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch):
    half = ("2025-03-10", "DIVIDENDOS CLIENTE AAAA3 S/ 100", 12.345)  # 12.34499… in binary
    assert run(monkeypatch, [half]) == 1
    assert run(monkeypatch, [half]) == 0
    assert ledger.dividends["total"].tolist() == ["12.35"]


def test_income_without_a_movement_date_fails():
    undated = [None, None, None, "DIVIDENDOS CLIENTE BBBB4 S/ 7", None, 0.70, 0.0]
    sheet = pd.DataFrame([statement_sheet([A]).iloc[0].tolist(), undated])
    with pytest.raises(ValueError, match="1 income line\\(s\\) without a movement date"):
        dp.parse_statement(sheet)


def test_income_dated_after_today_is_skipped_until_paid(capsys: pytest.CaptureFixture[str]):
    provisioned = ("2025-04-15", "JUROS S/ CAPITAL BBBB4 S/ 7", 0.70)
    out = dp.parse_statement(statement_sheet([A, BLANK, provisioned]), today=dt.date(2025, 3, 31))
    assert out["ticker"].tolist() == ["AAAA3"]
    assert "1 income line(s) dated after today (provisioned, not paid yet) skipped: 2025-04-15" in capsys.readouterr().out
    assert len(dp.parse_statement(statement_sheet([A, BLANK, provisioned]), today=dt.date(2025, 4, 15))) == 2


def test_an_untraded_ticker_is_asked_until_a_portfolio_is_given(
    ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch
):
    answers = iter(["", "   ", " growth "])  # blank answers ask again, never save a placeholder
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert run(monkeypatch, [("2025-03-10", "DIVIDENDOS CLIENTE EEEE3 S/ 10", 1.00)]) == 1
    assert ledger.dividends["portfolio"].tolist() == ["GROWTH"]


def test_ctrl_d_at_the_portfolio_prompt_saves_nothing(ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch):
    def closed(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    with pytest.raises(EOFError, match="nothing saved"):
        run(monkeypatch, [A, ("2025-03-10", "DIVIDENDOS CLIENTE EEEE3 S/ 10", 1.00)])
    assert ledger.dividends.empty and ledger.commits == 0  # not even the resolvable AAAA3 row
