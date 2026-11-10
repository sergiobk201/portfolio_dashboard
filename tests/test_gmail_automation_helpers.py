"""Nota importer (gmail_automation.py): Gmail batch loop, reconciliation, prompts, re-run guard and wiring.

The real importer runs end to end against in-memory fakes: a Gmail service,
the trades/assets tables, and "PDFs" whose decrypted text is the synthetic nota
itself. Libraries this environment lacks (the CI gate installs only the
pipeline stack) are replaced by import stubs, and every call into them is faked.
SYNTHETIC DATA ONLY — this repository is public.
"""

from __future__ import annotations

import ast
import base64
import csv
import datetime as dt
import importlib.util
import re
import sys
import types
import warnings
from decimal import Decimal
from pathlib import Path

import pandas as pd
import psycopg2
import pytest

from ledger_dedupe import TRADE_KEY, LedgerUnavailableError, as_cents, as_code, as_day, plan_idempotent_load

GMAIL_SCRIPT = Path(__file__).resolve().parents[1] / "pipelines" / "gmail_automation.py"
NOTA_DAY = dt.date(2025, 3, 10)


def _offline(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("stubbed library called — the test must fake this edge")


def _missing(name: str) -> bool:
    if name in sys.modules:
        return False
    try:
        return importlib.util.find_spec(name) is None
    except ModuleNotFoundError:  # a parent package is not installed
        return True


def _install_import_stubs() -> None:
    """Minimal stand-ins for the Gmail / PDF libraries that are not installed."""
    api_error = type("Error", (Exception,), {})
    auth_error = type("GoogleAuthError", (Exception,), {})
    pdf_error = type("PdfError", (Exception,), {})
    stubs: dict[str, dict[str, object]] = {
        "google.auth.transport.requests": {"Request": _offline},
        "google.oauth2.credentials": {"Credentials": type("Credentials", (), {"from_authorized_user_file": _offline})},
        "google_auth_oauthlib.flow": {"InstalledAppFlow": type("InstalledAppFlow", (), {})},
        "googleapiclient.discovery": {"build": _offline},
        "googleapiclient.errors": {"Error": api_error, "HttpError": type("HttpError", (api_error,), {})},
        "google.auth.exceptions": {"GoogleAuthError": auth_error, "RefreshError": type("RefreshError", (auth_error,), {})},
        "httplib2": {"HttpLib2Error": type("HttpLib2Error", (Exception,), {})},
        "pikepdf": {"PdfError": pdf_error, "PasswordError": type("PasswordError", (pdf_error,), {}), "open": _offline},
        "pdfplumber": {"open": _offline},
    }
    for name, attrs in stubs.items():
        if _missing(name):
            module = types.ModuleType(name)
            module.__dict__.update(attrs)
            sys.modules[name] = module


_install_import_stubs()
with warnings.catch_warnings():  # google-api-core warns about old Pythons at import time
    warnings.simplefilter("ignore", FutureWarning)
    import gmail_automation as gm  # noqa: E402


# ── synthetic notas and e-mails ─────────────────────────────────────────────


def br(value: float) -> str:
    """1234.5 → '1.234,50' (Brazilian number format)."""
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def dc(value: float) -> str:
    """Sinacor D/C flag of a signed amount (debit positive)."""
    return "D" if value >= 0 else "C"


def nota_text(day: str, *lines: tuple[str, str, int, float], costs: bool = True, summary: bool = True) -> str:
    """Synthetic Sinacor nota as pdfplumber extracts it, every figure consistent with its lines.

    Header with the trading day, '1-BOVESPA' lines, then the bottom block:
    "Resumo dos Negócios" (left) and "Resumo Financeiro" (right) merged line by
    line, ending with the settlement-day "Líquido para" line.

    Args:
        day: Trading day, dd/mm/yyyy.
        lines: (side, asset, quantity, price) per trade line.
        costs: False drops the whole bottom block.
        summary: False drops only the "Resumo dos Negócios" column (a layout change).
    """
    body = [
        f"1-BOVESPA {side} VISTA {asset} @ {qty} {br(price)} {br(qty * price)} {'D' if side == 'C' else 'C'}"
        for side, asset, qty, price in lines
    ]
    buys = round(sum(round(qty * price, 2) for side, _, qty, price in lines if side == "C"), 2)
    sells = round(sum(round(qty * price, 2) for side, _, qty, price in lines if side == "V"), 2)
    net = round(buys - sells, 2)
    cblc = round(net + 0.25, 2)  # net + taxa de liquidação, debit positive
    net_cash = round(cblc + 0.04, 2)  # + emolumentos
    settle = (dt.datetime.strptime(day, "%d/%m/%Y") + dt.timedelta(days=2)).strftime("%d/%m/%Y")
    rows = [
        ("Resumo dos Negócios", "Resumo Financeiro"),
        ("Debêntures 0,00", "Clearing"),
        (f"Vendas à vista {br(sells)}", f"Valor líquido das operações {br(abs(net))} {dc(net)}"),
        (f"Compras à vista {br(buys)}", "Taxa de liquidação 0,25 D"),
        ("Opções - compras 0,00", "Taxa de Registro 0,00 D"),
        ("Opções - vendas 0,00", f"Total CBLC {br(abs(cblc))} {dc(cblc)}"),
        ("Operações à termo 0,00", "Bolsa"),
        (f"Valor das operações {br(buys + sells)}", "Emolumentos 0,04 D"),
        ("", "Total Bovespa / Soma 0,04 D"),
        ("", "Total Custos / Despesas 0,00 D"),
        ("", f"Líquido para {settle} {br(abs(net_cash))} {dc(net_cash)}"),
    ]
    block = [f"{left if summary else ''} {right}".strip() for left, right in rows] if costs else []
    header = [
        "NOTA DE CORRETAGEM",
        "Nr. nota Folha Data pregão",
        f"123456 1 {day}",
        "Negócios realizados",
        "Q Negociação C/V Tipo mercado Prazo Especificação do título Obs. (*) Quantidade Preço / Ajuste "
        "Valor Operação / Ajuste D/C",
    ]
    return "\n".join([*header, *body, *block]) + "\n"


BUY_A = ("C", "AAAA ON", 100, 12.34)
SELL_B = ("V", "BBBB PN", 7, 61.50)
FILL_C = ("C", "CCCC ON", 2, 10.00)


class _Call:
    def __init__(self, value: object = None, error: Exception | None = None) -> None:
        self.value, self.error = value, error

    def execute(self) -> object:
        if self.error:
            raise self.error
        return self.value


class FakeGmail:
    """users().messages()[.attachments()] of the Gmail API over in-memory e-mails.

    ``listing`` is the order Gmail returns ids in — deliberately not
    chronological in the tests; only ``internalDate`` may decide the order.
    """

    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size
        self.emails: dict[str, dict] = {}
        self.listing: list[str] = []
        self.content: dict[str, bytes] = {}
        self.broken: set[str] = set()  # attachment ids whose download times out
        self.list_calls: list[dict] = []
        self.downloads: list[str] = []

    def add(self, msg_id: str, received: str, *pdf_texts: str, logo: bool = True, inline: bool = False) -> "FakeGmail":
        parts: list[dict] = [{"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "filename": "", "body": {"size": 4, "data": "Tm90YQ=="}}]}]
        if logo:
            parts.append({"mimeType": "image/png", "filename": "logo.png", "body": {"attachmentId": f"{msg_id}-logo"}})
        for n, text in enumerate(pdf_texts, start=1):
            att_id, data = f"{msg_id}-pdf{n}", text.encode("utf-8")
            body = {"data": base64.urlsafe_b64encode(data).decode()} if inline else {"attachmentId": att_id}
            parts.append({"mimeType": "application/pdf", "filename": f"NotaCorretagem_{n}.PDF", "body": body})
            self.content[att_id] = data
        stamp = int(pd.Timestamp(received, tz="America/Sao_Paulo").timestamp() * 1000)
        self.emails[msg_id] = {"id": msg_id, "internalDate": str(stamp), "payload": {"mimeType": "multipart/mixed", "parts": parts}}
        self.listing.append(msg_id)
        return self

    def users(self) -> "FakeGmail":
        return self

    def messages(self) -> "FakeGmail":
        return self

    def attachments(self) -> "FakeGmail":
        return self

    def list(self, **request: str) -> _Call:
        self.list_calls.append(request)
        page = int(request.get("pageToken", "0"))
        ids = self.listing[page * self.page_size : (page + 1) * self.page_size]
        response: dict[str, object] = {"resultSizeEstimate": len(ids)}
        if ids:
            response["messages"] = [{"id": i, "threadId": i} for i in ids]
        if (page + 1) * self.page_size < len(self.listing):
            response["nextPageToken"] = str(page + 1)
        return _Call(response)

    def get(self, userId: str, id: str, format: str | None = None, messageId: str | None = None) -> _Call:  # noqa: A002
        if messageId is None:  # messages().get
            return _Call(self.emails[id])
        self.downloads.append(id)  # attachments().get
        if id in self.broken:
            return _Call(error=TimeoutError("attachment download timed out"))
        return _Call({"data": base64.urlsafe_b64encode(self.content[id]).decode()})


class FakeDB:
    """trades + assets held in memory behind psycopg2.connect / pandas.read_sql_query.

    Like Postgres, COPY rows stay pending until commit (rollback or close
    discards them), and every row must carry exactly one field per column.
    """

    SEEDED = 3

    def __init__(self) -> None:
        self.trades = pd.DataFrame(
            {
                "trade_id": [f"{i:032x}" for i in range(self.SEEDED)],
                "date": [dt.date(2025, 1, 6)] * self.SEEDED,
                "side": ["BUY"] * self.SEEDED,
                "asset": ["AAAA ON", "BBBB PN", "CCCC ON"],
                "ticker": ["AAAA3", "BBBB4", "CCCC3"],
                "quantity": [1] * self.SEEDED,
                "price": [10.0] * self.SEEDED,
                "total": [10.0] * self.SEEDED,
                "fee": [0.0] * self.SEEDED,
                "portfolio": ["ALPHA", "BETA", "ALPHA"],
            }
        )
        self.assets = pd.DataFrame({"ticker": ["AAAA3", "BBBB4", "CCCC3"], "company_name": ["A", "B", "C"]})
        self.fail_insert = self.fail_read = False
        self.fail_table: str | None = None  # COPY into this table fails
        self.pending: list[tuple[str, pd.DataFrame]] = []
        self.commits = self.rollbacks = 0

    @property
    def loaded(self) -> pd.DataFrame:
        """Rows the importer inserted."""
        return self.trades.iloc[self.SEEDED :]

    def connect(self, **_: object) -> "FakeDB":
        return self

    def cursor(self) -> "FakeDB":
        return self

    def __enter__(self) -> "FakeDB":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def commit(self) -> None:
        for table, rows in self.pending:
            setattr(self, table, pd.concat([getattr(self, table).astype(str), rows], ignore_index=True))
        self.pending.clear()
        self.commits += 1

    def rollback(self) -> None:
        self.pending.clear()
        self.rollbacks += 1

    def close(self) -> None:
        self.pending.clear()  # uncommitted rows die with the connection

    def _stage(self, table: str, columns: list[str], rows: list[list[str]]) -> None:
        if self.fail_insert or self.fail_table == table:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        if any(len(row) != len(columns) for row in rows):
            raise psycopg2.DataError(f"COPY {table}: extra data after last expected column")
        self.pending.append((table, pd.DataFrame(rows, columns=columns, dtype=str)))

    def copy_expert(self, sql: str, file) -> None:
        match = re.fullmatch(r"COPY (\w+) \((\w+(?:, \w+)*)\) FROM STDIN WITH \(FORMAT csv\)", sql)
        assert match, f"unexpected COPY statement: {sql}"
        self._stage(match[1], match[2].split(", "), list(csv.reader(file)))

    def copy_from(self, file, table: str, sep: str = "\t", columns: list[str] | None = None, **_: object) -> None:
        """Text-format COPY, as copy_from sends it: no CSV quoting, every separator splits."""
        self._stage(table, list(columns or []), [line.split(sep) for line in file.read().splitlines()])

    def read_sql_query(self, sql: str, _connection: object) -> pd.DataFrame:
        if self.fail_read:
            raise psycopg2.OperationalError("could not connect to server")
        if "FROM assets" in sql:
            return self.assets.copy()
        columns = [c.strip() for c in sql.split("SELECT")[1].split("FROM")[0].split(",")]
        out = self.trades[columns].astype(object)  # psycopg2 types: DATE → date, NUMERIC → Decimal
        out["date"] = [pd.Timestamp(v).date() for v in self.trades["date"]]
        out["total"] = [Decimal(str(v)).quantize(Decimal("0.01")) for v in self.trades["total"]]
        out["quantity"] = [int(v) for v in self.trades["quantity"]]
        return out


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network, database, prompts or real PDFs anywhere in this module."""

    def no_database(**_: object) -> None:
        raise AssertionError("real database connection attempted")

    def no_prompt(prompt: str = "") -> str:
        raise AssertionError(f"unexpected prompt: {prompt}")

    def decrypt(self: gm.GmailRicoImporter, src: Path, dst: Path, password: str) -> None:
        Path(dst).write_bytes(Path(src).read_bytes())

    monkeypatch.setattr(gm.psycopg2, "connect", no_database)
    monkeypatch.setattr("builtins.input", no_prompt)
    monkeypatch.setattr(gm.yf, "Ticker", _offline)
    monkeypatch.setattr(gm.GmailRicoImporter, "decrypt_pdf", decrypt)
    monkeypatch.setattr(gm, "pdf_text", lambda path, max_pages=5: Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB()
    monkeypatch.setattr(gm.psycopg2, "connect", fake.connect)
    monkeypatch.setattr(gm.pd, "read_sql_query", fake.read_sql_query)
    return fake


# ── Gmail listing, files and the batch loop ────────────────────────────────


def test_listing_reads_every_page_and_orders_by_internal_date():
    gmail = FakeGmail(page_size=2)
    for msg_id, received in [("m-c", "2025-03-12 18:00"), ("m-a", "2025-03-10 18:00"), ("m-e", "2025-03-14 18:00"),
                             ("m-b", "2025-03-10 23:30"), ("m-d", "2025-03-13 09:00")]:
        gmail.add(msg_id, received, "pdf")
    messages = gm.list_nota_messages(gmail, gm.nota_query(45))
    assert [m["id"] for m in messages] == ["m-a", "m-b", "m-c", "m-d", "m-e"]
    assert [call.get("pageToken") for call in gmail.list_calls] == [None, "1", "2"]
    assert {call["q"] for call in gmail.list_calls} == {"from: noreply@rico.com.vc has:attachment newer_than:45d"}
    assert not any("maxResults" in call for call in gmail.list_calls)
    assert gm.received_day(messages[1]) == "2025-03-10"  # 23:30 in São Paulo is already the 11th in UTC
    assert gm.list_nota_messages(FakeGmail(), "q") == []  # an empty window is not an error


def test_each_pdf_is_saved_under_its_message_id(tmp_path: Path):
    gmail = FakeGmail().add("m-1", "2025-03-10", "first").add("m-2", "2025-03-11", "second", "third")
    gmail.add("m-3", "2025-03-12", "inline", inline=True)
    saved = {m["id"]: gm.save_nota_pdfs(gmail, m, tmp_path) for m in gm.list_nota_messages(gmail, "q")}
    assert [p.name for p in saved["m-1"]] == ["trade_confirmation_m-1_1.pdf"]
    assert [p.name for p in saved["m-2"]] == ["trade_confirmation_m-2_1.pdf", "trade_confirmation_m-2_2.pdf"]
    assert [p.read_text() for p in saved["m-2"]] == ["second", "third"]
    assert saved["m-3"][0].read_text() == "inline"
    assert not any(att.endswith("-logo") for att in gmail.downloads)  # non-PDF parts are ignored
    assert "m-3-pdf1" not in gmail.downloads  # inline data needs no download


def test_a_stale_file_is_overwritten_never_reused(tmp_path: Path):
    stale = tmp_path / "trade_confirmation_m-1_1.pdf"
    stale.write_text("left by an earlier run")
    gmail = FakeGmail().add("m-1", "2025-03-10", "fresh")
    assert gm.save_nota_pdfs(gmail, gm.list_nota_messages(gmail, "q")[0], tmp_path) == [stale]
    assert stale.read_text() == "fresh"


def test_a_failed_download_stops_the_batch_and_never_falls_back_to_a_stale_file(db: FakeDB, tmp_path: Path):
    stale = tmp_path / "trade_confirmation_m-2_1.pdf"
    stale.write_text(nota_text("11/03/2025", SELL_B))  # a loadable nota from an earlier run
    gmail = FakeGmail()
    gmail.add("m-1", "2025-03-10 18:00", nota_text("10/03/2025", BUY_A))
    gmail.add("m-2", "2025-03-11 18:00", nota_text("11/03/2025", SELL_B))
    gmail.add("m-3", "2025-03-12 18:00", nota_text("12/03/2025", FILL_C))
    gmail.broken.add("m-2-pdf1")
    results = gm.import_notas(gmail, attachments_dir=tmp_path)
    assert [(r.message_id, r.status) for r in results] == [("m-1", gm.LOADED), ("m-2", gm.FAILED)]
    assert "download failed" in results[-1].error
    assert "m-3-pdf1" not in gmail.downloads  # stopped at the failure
    assert db.loaded["ticker"].tolist() == ["AAAA3"]  # the stale m-2 file was not loaded
    assert stale.exists()  # untouched: only files this run wrote are processed or deleted


def test_the_batch_stops_at_the_first_failing_nota_and_names_it(
    db: FakeDB, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    gmail = FakeGmail()
    gmail.add("m-1", "2025-03-10 18:00", nota_text("10/03/2025", BUY_A))
    gmail.add("m-2", "2025-03-11 18:00", nota_text("11/03/2025", SELL_B, costs=False))  # no cost block
    gmail.add("m-3", "2025-03-12 18:00", nota_text("12/03/2025", FILL_C))
    monkeypatch.setattr(gm, "gmail_service", lambda: gmail)
    monkeypatch.setattr(gm, "ATTACHMENTS_DIR", tmp_path)
    assert gm.main([]) == 1
    out = capsys.readouterr().out
    assert "Stopped at nota 2025-03-11 — e-mail m-2, received 2025-03-11: ValueError: nota text has no" in out
    assert "m-3-pdf1" not in gmail.downloads
    assert db.loaded["date"].tolist() == ["2025-03-10"] and db.commits == 1
    assert not list(tmp_path.glob("*.pdf"))  # the failed nota's PDFs are deleted too


def test_a_pdf_the_password_does_not_open_stops_the_batch(db: FakeDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def locked(self: gm.GmailRicoImporter, src: Path, dst: Path, password: str) -> None:
        raise gm.pikepdf.PasswordError(f"{src}: invalid password")

    monkeypatch.setattr(gm.GmailRicoImporter, "decrypt_pdf", locked)
    gmail = FakeGmail().add("m-1", "2025-03-10", "pdf").add("m-2", "2025-03-11", "pdf")
    results = gm.import_notas(gmail, attachments_dir=tmp_path)
    assert [(r.message_id, r.status) for r in results] == [("m-1", gm.FAILED)]
    assert "PasswordError: trade_confirmation_m-1_1.pdf: PASSWORD_PDF (.env) does not open it" in results[0].error
    assert gmail.downloads == ["m-1-pdf1"] and not list(tmp_path.iterdir())


def test_a_failed_insert_stops_the_batch(db: FakeDB, tmp_path: Path):
    gmail = FakeGmail().add("m-1", "2025-03-10", nota_text("10/03/2025", BUY_A))
    gmail.add("m-2", "2025-03-11", nota_text("11/03/2025", SELL_B))
    db.fail_insert = True
    results = gm.import_notas(gmail, attachments_dir=tmp_path)
    assert [r.status for r in results] == [gm.FAILED] and "OperationalError" in results[0].error
    assert db.rollbacks == 1 and db.loaded.empty and gmail.downloads == ["m-1-pdf1"]


def test_an_unreadable_trades_table_stops_the_batch_before_any_write(db: FakeDB, tmp_path: Path):
    gmail = FakeGmail().add("m-1", "2025-03-10", nota_text("10/03/2025", BUY_A))
    gmail.add("m-2", "2025-03-11", nota_text("11/03/2025", SELL_B))
    db.fail_read = True
    results = gm.import_notas(gmail, attachments_dir=tmp_path)
    assert [(r.trade_date, r.status) for r in results] == [("2025-03-10", gm.FAILED)]
    assert "OperationalError" in results[0].error
    assert db.loaded.empty and db.commits == 0 and gmail.downloads == ["m-1-pdf1"]


def test_a_second_run_inserts_nothing(db: FakeDB, tmp_path: Path):
    gmail = FakeGmail()
    gmail.add("m-2", "2025-03-11 18:00", nota_text("11/03/2025", SELL_B, *[FILL_C] * 3))  # identical fills
    gmail.add("m-1", "2025-03-10 18:00", nota_text("10/03/2025", BUY_A))
    first = gm.import_notas(gmail, attachments_dir=tmp_path)
    assert [(r.trade_date, r.status, r.inserted) for r in first] == [
        ("2025-03-10", gm.LOADED, 1),
        ("2025-03-11", gm.LOADED, 4),
    ]
    second = gm.import_notas(gmail, attachments_dir=tmp_path)
    assert [(r.status, r.inserted, r.skipped) for r in second] == [(gm.ALREADY_LOADED, 0, 1), (gm.ALREADY_LOADED, 0, 4)]
    assert len(db.loaded) == 5 and db.commits == 2


def test_a_resent_nota_is_loaded_once(db: FakeDB, tmp_path: Path):
    text = nota_text("10/03/2025", BUY_A, SELL_B)
    gmail = FakeGmail().add("m-1", "2025-03-10 18:00", text).add("m-2", "2025-03-10 19:00", text)
    results = gm.import_notas(gmail, attachments_dir=tmp_path)
    # Nota 2 re-reads the table, so it sees nota 1's rows.
    assert [(r.status, r.inserted, r.skipped) for r in results] == [(gm.LOADED, 2, 0), (gm.ALREADY_LOADED, 0, 2)]
    assert len(db.loaded) == 2


def test_pdfs_are_deleted_after_each_nota_unless_kept(db: FakeDB, tmp_path: Path):
    gmail = FakeGmail().add("m-1", "2025-03-10", nota_text("10/03/2025", BUY_A))
    gm.import_notas(gmail, attachments_dir=tmp_path / "default")
    assert not list((tmp_path / "default").iterdir())
    gm.import_notas(gmail, keep_pdfs=True, attachments_dir=tmp_path / "kept")
    assert sorted(p.name for p in (tmp_path / "kept").iterdir()) == [
        "statement_unlocked_m-1_1.pdf",
        "trade_confirmation_m-1_1.pdf",
    ]


def test_e_mails_without_trades_are_reported_and_the_batch_continues(db: FakeDB, tmp_path: Path):
    gmail = FakeGmail()
    gmail.add("m-1", "2025-03-09", logo=True)  # no PDF at all
    gmail.add("m-2", "2025-03-10", "INFORME DE RENDIMENTOS 2024\n")  # a PDF with no Bovespa lines
    gmail.add("m-3", "2025-03-11", nota_text("11/03/2025", BUY_A))
    results = gm.import_notas(gmail, attachments_dir=tmp_path)
    assert [r.status for r in results] == [gm.NO_PDF, gm.NO_TRADES, gm.LOADED]
    assert db.loaded["ticker"].tolist() == ["AAAA3"]


# ── the nota against its own printout ──────────────────────────────────────


def import_one(text: str, folder: Path) -> gm.NotaResult:
    """Run one e-mailed nota through the batch loop; return its result."""
    (result,) = gm.import_notas(FakeGmail().add("m-1", "2025-03-10 18:00", text), attachments_dir=folder)
    return result


def test_a_missed_trade_line_fails_the_nota_and_names_both_figures(db: FakeDB, tmp_path: Path):
    # The FILL_C line is printed (and in "Compras à vista") but not recognised.
    text = nota_text("10/03/2025", BUY_A, FILL_C).replace("1-BOVESPA C VISTA CCCC", " 1-BOVESPA C VISTA CCCC")
    result = import_one(text, tmp_path)
    assert (result.trade_date, result.status) == ("2025-03-10", gm.FAILED)
    assert (
        "nota 10/03/2025: trade lines add up to 1.234,00 but 'Compras à vista' prints 1.254,00 — "
        "a trade line was missed or misread. Nothing loaded."
    ) in result.error
    assert db.loaded.empty and db.commits == 0


def test_totals_must_match_to_the_cent_on_each_side():
    text = nota_text("10/03/2025", BUY_A, SELL_B)
    lines = gm.parse_trade_lines(text)
    gm.reconcile_with_summary(text, lines, "10/03/2025")  # consistent: no error
    with pytest.raises(ValueError, match="add up to 430,50 but 'Vendas à vista' prints 430,51"):
        gm.reconcile_with_summary(text.replace("Vendas à vista 430,50", "Vendas à vista 430,51"), lines, "10/03/2025")
    decomposed = text.replace("Compras à vista", "Compras à vista")  # 'à' extracted as a + combining grave
    gm.reconcile_with_summary(decomposed, lines, "10/03/2025")


def test_missing_printed_totals_fail_as_a_layout_change(db: FakeDB, tmp_path: Path):
    result = import_one(nota_text("10/03/2025", BUY_A, summary=False), tmp_path)
    assert result.status == gm.FAILED
    assert "nota 10/03/2025: no printed 'Compras à vista' total — nota layout changed? Nothing loaded." in result.error
    assert db.loaded.empty


@pytest.mark.parametrize(
    ("text", "days"),
    [
        (nota_text("10/03/2025", BUY_A) + nota_text("11/03/2025", SELL_B), "10/03/2025, 11/03/2025"),
        # A trade-less (BM&F) nota ahead of the Bovespa one: its date must not date the trades.
        ("NOTA DE CORRETAGEM\nData pregão 07/03/2025\nC WDO F25 1 5.000,000 DAY TRADE\n" + nota_text("10/03/2025", BUY_A),
         "07/03/2025, 10/03/2025"),
    ],
)
def test_a_pdf_spanning_two_trading_days_fails(db: FakeDB, tmp_path: Path, text: str, days: str):
    result = import_one(text, tmp_path)
    assert result.status == gm.FAILED and f"the PDF spans 2 trading days ({days})" in result.error
    assert db.loaded.empty


def test_a_multi_page_nota_repeats_its_header_and_loads_under_one_day(db: FakeDB, tmp_path: Path):
    page_break = "Líquido para 12/03/2025 CONTINUA...\nNOTA DE CORRETAGEM\nNr. nota Folha Data pregão\n123456 2 10/03/2025\n"
    text = nota_text("10/03/2025", BUY_A, SELL_B).replace("1-BOVESPA V", page_break + "1-BOVESPA V")
    result = import_one(text, tmp_path)
    assert (result.trade_date, result.status, result.inserted) == ("2025-03-10", gm.LOADED, 2)


def test_a_rerun_reconciles_every_nota_before_skipping_it(db: FakeDB, tmp_path: Path):
    text = nota_text("10/03/2025", BUY_A, SELL_B)
    assert import_one(text, tmp_path).status == gm.LOADED
    rerun = import_one(text, tmp_path)
    assert (rerun.status, rerun.inserted, rerun.skipped) == (gm.ALREADY_LOADED, 0, 2)
    misread = import_one(text.replace("Vendas à vista 430,50", "Vendas à vista 430,51"), tmp_path)
    assert misread.status == gm.FAILED and "'Vendas à vista' prints 430,51" in misread.error
    assert len(db.loaded) == 2 and db.commits == 1


def test_a_bovespa_nota_with_no_recognised_line_fails_instead_of_no_trades(db: FakeDB, tmp_path: Path):
    text = nota_text("10/03/2025", BUY_A).replace("1-BOVESPA", "1-B3")  # a renamed market column
    result = import_one(text, tmp_path)
    assert result.status == gm.FAILED
    assert "prints a Bovespa summary but no '1-BOVESPA' line was recognised" in result.error
    assert gm.BOVESPA_SECTION.search("INFORME DE RENDIMENTOS 2024") is None  # stays NO_TRADES (test above)


def test_implausible_costs_fail_the_nota(db: FakeDB, tmp_path: Path):
    # A sell nota whose CBLC credit is read as a debit: fees ≈ twice the proceeds.
    text = nota_text("10/03/2025", SELL_B).replace("Total CBLC 430,25 C", "Total CBLC 430,25 D")
    result = import_one(text, tmp_path)
    assert result.status == gm.FAILED and "nota costs 860.79 exceed the 14.30 cap" in result.error
    assert db.loaded.empty


# ── prompts and the single write ───────────────────────────────────────────

NEW_D = ("C", "DDDD ON", 10, 5.00)  # an asset the ledger has never seen


def scripted_input(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Answer prompts in order, then behave like a closed stdin; return the prompts shown."""
    queue, prompts = list(answers), []

    def fake(prompt: str = "") -> str:
        prompts.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake)
    return prompts


@pytest.fixture
def yahoo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sector / industry lookup; the industry holds a comma, as Yahoo's often do."""
    info = {"sector": "Consumer Cyclical", "industry": "Furnishings, Fixtures & Appliances"}
    monkeypatch.setattr(gm.yf, "Ticker", lambda _symbol: types.SimpleNamespace(info=info))


def test_a_new_asset_reprompts_until_a_valid_ticker_and_lands_with_its_trade(
    db: FakeDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yahoo: None
):
    prompts = scripted_input(monkeypatch, "", "dddd", "DDDD ON", " dddd3f ", "gamma")
    result = import_one(nota_text("10/03/2025", NEW_D), tmp_path)
    assert result.status == gm.LOADED
    assert prompts == ["Enter ticker for 'DDDD ON': "] * 4 + ["[0] Enter Portfolio for 'DDDD3': "]
    assert db.loaded[["ticker", "portfolio"]].values.tolist() == [["DDDD3", "GAMMA"]]  # F suffix → round lot
    assert db.assets.iloc[-1].tolist() == ["DDDD3", "DDDD ON", "Consumer Cyclical", "Furnishings, Fixtures & Appliances"]
    assert db.commits == 1  # assets and trades in one transaction


@pytest.mark.parametrize("answers", [(), ("DDDD3",)], ids=["eof-at-ticker", "eof-at-portfolio"])
def test_a_closed_stdin_stops_the_nota_with_nothing_saved(
    db: FakeDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yahoo: None, answers: tuple[str, ...]
):
    scripted_input(monkeypatch, *answers)
    result = import_one(nota_text("10/03/2025", NEW_D), tmp_path)
    assert result.status == gm.FAILED
    assert result.error.startswith("EOFError: no answer to") and "nothing saved" in result.error
    assert db.loaded.empty and len(db.assets) == 3 and db.commits == 0
    assert not db.trades.isin([gm.UNRESOLVED_PLACEHOLDER]).any().any()


@pytest.mark.parametrize("failing", ["assets", "trades"])
def test_a_failed_insert_saves_neither_assets_nor_trades(
    db: FakeDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yahoo: None, failing: str
):
    scripted_input(monkeypatch, "DDDD3", "GAMMA")
    db.fail_table = failing
    result = import_one(nota_text("10/03/2025", NEW_D), tmp_path)
    assert result.status == gm.FAILED and "OperationalError" in result.error  # re-raised, not printed
    assert db.loaded.empty and len(db.assets) == 3 and (db.commits, db.rollbacks) == (0, 1)


@pytest.mark.parametrize(
    ("answer", "ticker"),
    [("abcd3", "ABCD3"), (" ABCD11 ", "ABCD11"), ("abcd4f", "ABCD4"), ("ABCD11F", "ABCD11"),
     ("A1BC3", "A1BC3"), ("ABCD34", "ABCD34"),  # a digit in the root, BDR suffix
     ("", None), ("ABCD", None), ("ABC3", None), ("ABCDE3", None), ("ABCD123", None), ("1ABC3", None),
     ("ABCD3.SA", None), ("ABCD 3", None), ("ABCD ON", None)],
)
def test_b3_ticker_answers(answer: str, ticker: str | None):
    assert gm.b3_ticker(answer) == ticker


def test_cli_options():
    assert (gm.parse_args([]).days, gm.parse_args([]).keep_pdfs) == (45, False)
    args = gm.parse_args(["--days", "10", "--keep-pdfs"])
    assert (args.days, args.keep_pdfs) == (10, True)
    assert gm.nota_query(args.days).endswith("newer_than:10d")
    for bad in (["--days", "0"], ["--days", "ten"]):
        with pytest.raises(SystemExit):
            gm.parse_args(bad)


def test_summary_has_one_line_per_nota_and_a_total_with_counts_only():
    results = [
        gm.NotaResult("m-1", "2025-03-10", "2025-03-10", gm.LOADED, inserted=3),
        gm.NotaResult("m-2", "2025-03-11", "2025-03-11", gm.ALREADY_LOADED, skipped=2),
    ]
    lines = gm.summary_lines(results)
    assert len(lines) == 3 and "m-2" in lines[1] and "inserted   0  skipped   2" in lines[1]
    assert lines[-1] == "TOTAL 2 nota(s): inserted 3, skipped 2 (already loaded)"


def test_trade_lines_parse_brazilian_numbers():
    out = gm.parse_trade_lines(nota_text("10/03/2025", ("C", "AAAA ON NM", 1500, 9.87), SELL_B))
    assert out[["side", "asset", "quantity"]].values.tolist() == [["C", "AAAA ON NM", 1500], ["V", "BBBB PN", 7]]
    assert out["total"].tolist() == pytest.approx([14805.0, 430.5])
    assert gm.parse_trade_lines("no trades here").columns.tolist() == gm.NOTA_COLUMNS


def test_thousands_separators_in_quantity_and_price_parse_exactly():
    # Synthetic line: 2.500 shares at 1.234,56 (both printed with a thousands dot).
    out = gm.parse_trade_lines("1-BOVESPA C VISTA DDDD ON NM @ 2.500 1.234,56 3.086.400,00 D\n")
    assert out[["asset", "quantity"]].values.tolist() == [["DDDD ON NM", 2500]]
    assert out["price"].tolist() == pytest.approx([1234.56]) and out["total"].tolist() == pytest.approx([3086400.0])


def test_an_averaged_price_within_a_cent_reconciles():
    # Three fills averaged into one line: 10.004 printed as 10,00 on 300 shares.
    out = gm.parse_trade_lines("1-BOVESPA V VISTA EEEE PN @ 300 10,00 3.001,20 C\n")
    assert out["price"].tolist() == pytest.approx([10.0]) and out["total"].tolist() == pytest.approx([3001.2])


@pytest.mark.parametrize(
    "line",
    [
        "1-BOVESPA C TERMO FFFF ON 30 100 12,00 1.200,00 D",  # termo: not a cash-market trade
        "1-BOVESPA C OPCAO DE COMPRA 03/25 FFFFC120 ON 12,00 @ 100 0,50 50,00 D",  # options
        "1-BOVESPA C VISTA GGGG ON @ 100 12,00 120,00 D",  # total disagrees with quantity × price
        "1-BOVESPA C VISTA HHHH ON @ 1.000,00 D",  # no quantity column at all
        "1-BOVESPA C VISTA @ 100 12,00 1.200,00 D",  # no asset name
    ],
)
def test_a_line_that_does_not_reconcile_stops_the_nota(line: str):
    with pytest.raises(ValueError, match="trade line 1"):
        gm.parse_trade_lines(line + "\n")


@pytest.mark.parametrize(
    ("line", "asset", "quantity"),
    [
        ("1-BOVESPA C VISTA DDDD ON 2 100 12,00 1.200,00 D", "DDDD ON", 100),  # observation code '2'
        ("1-BOVESPA V VISTA DDDD ON NM 8 1.000 1,20 1.200,00 C", "DDDD ON NM", 1000),  # '8', thousands dot
        ("1-BOVESPA C VISTA DDDD 11 CI @ 10 120,00 1.200,00 D", "DDDD 11 CI", 10),  # a number in the name
        ("1-BOVESPA C FRACIONARIO DDDD11 CI ER # 3 400,00 1.200,00 D", "DDDD11 CI ER", 3),
    ],
)
def test_quantity_price_and_total_are_read_from_the_right(line: str, asset: str, quantity: int):
    out = gm.parse_trade_lines(line + "\n")
    assert out[["asset", "quantity"]].values.tolist() == [[asset, quantity]]
    assert out["total"].tolist() == pytest.approx([1200.0])


# ── duplicate guard on importer-shaped frames ──────────────────────────────


def nota(lines: list[tuple[str, str, int, float]], day: dt.date = NOTA_DAY) -> pd.DataFrame:
    """(side, ticker, quantity, price) → rows as resolve_trades_df leaves them."""
    return pd.DataFrame(
        [
            {
                "date": day,
                "side": side,
                "ticker": ticker,
                "asset": f"{ticker} ON",
                "quantity": qty,
                "price": price,
                "total": round(qty * price, 2),
                "fee": round(qty * price * 3e-4, 2),
                "portfolio": None,
            }
            for side, ticker, qty, price in lines
        ],
        columns=["date", "side", "ticker", "asset", "quantity", "price", "total", "fee", "portfolio"],
    )


def stored(frame: pd.DataFrame, portfolio: str = "ALPHA") -> pd.DataFrame:
    """What SELECT … FROM trades returns through psycopg2: DATE → date, NUMERIC → Decimal."""
    out = frame.astype(object)
    out["total"] = [Decimal(f"{v:.2f}") for v in frame["total"]]
    out["price"] = [Decimal(f"{v:.4f}") for v in frame["price"]]
    out["trade_id"] = [f"{i:032x}" for i in range(len(frame))]
    out["portfolio"] = portfolio
    return out


def test_rerun_of_a_loaded_nota_inserts_nothing():
    lines = nota([("BUY", "AAAA3", 100, 12.34), ("SELL", "BBBB4", 7, 61.5)])
    plan = plan_idempotent_load(lines, stored(lines), TRADE_KEY)
    assert plan.new.empty and len(plan.skipped) == 2


def test_empty_table_and_a_new_session_insert_everything():
    lines = nota([("BUY", "AAAA3", 100, 12.34)])
    assert len(plan_idempotent_load(lines, stored(lines.iloc[:0]), TRADE_KEY).new) == 1
    next_day = nota([("BUY", "AAAA3", 100, 12.34)], day=dt.date(2025, 3, 11))
    assert len(plan_idempotent_load(next_day, stored(lines), TRADE_KEY).new) == 1


def test_identical_fills_load_once_and_are_skipped_on_rerun():
    fills = nota([("BUY", "AAAA3", 2, 10.0)] * 3)  # identical fills at one price
    assert len(plan_idempotent_load(fills, stored(fills.iloc[:0]), TRADE_KEY).new) == 3  # not collapsed
    assert plan_idempotent_load(fills, stored(fills), TRADE_KEY).new.empty
    # Multiset: one stored copy absorbs exactly one incoming copy.
    assert len(plan_idempotent_load(fills, stored(fills.iloc[:1]), TRADE_KEY).new) == 2


def test_partial_overlap_keeps_the_unmatched_lines_in_nota_order():
    lines = nota([("BUY", "AAAA3", 100, 12.34), ("BUY", "CCCC3", 5, 4.2), ("SELL", "BBBB4", 7, 61.5)])
    plan = plan_idempotent_load(lines, stored(lines.iloc[[1]]), TRADE_KEY)
    assert plan.new["ticker"].tolist() == ["AAAA3", "BBBB4"]
    assert plan.new.index.tolist() == [0, 2]  # labels survive for the portfolio prompt
    assert plan.skipped["ticker"].tolist() == ["CCCC3"]


def test_rederived_fee_assigned_portfolio_and_stored_precision_do_not_defeat_the_guard():
    lines = nota([("BUY", "AAAA3", 3, 33.33)])
    db = stored(lines, portfolio="BETA")
    db["fee"] = Decimal("0.99")  # an older fee formula
    assert plan_idempotent_load(lines, db, TRADE_KEY).new.empty


def test_side_quantity_and_total_tell_lines_apart():
    db = stored(nota([("BUY", "AAAA3", 100, 12.34)]))
    for other in (("SELL", "AAAA3", 100, 12.34), ("BUY", "AAAA3", 101, 12.34), ("BUY", "AAAA3", 100, 12.35)):
        assert len(plan_idempotent_load(nota([other]), db, TRADE_KEY).new) == 1


def test_a_failed_ledger_read_fails_closed():
    with pytest.raises(LedgerUnavailableError, match="refusing to load blind"):
        plan_idempotent_load(nota([("BUY", "AAAA3", 1, 1.0)]), pd.DataFrame(), TRADE_KEY)


def test_normalizers_make_parser_and_database_values_comparable():
    assert as_code(pd.Series([" aaaa3 ", "FRAÇÕES"])).tolist() == ["AAAA3", "FRAÇÕES"]
    assert as_cents(pd.Series([Decimal("1234.50"), 1234.5, 0.1 + 0.2], dtype=object)).tolist() == [123450, 123450, 30]
    days = [pd.Series([dt.date(2025, 3, 10)]), pd.Series(["2025-03-10"]), pd.Series([pd.Timestamp("2025-03-10 15:30")])]
    assert {as_day(s).iloc[0] for s in days} == {pd.Timestamp("2025-03-10")}


# ── static wiring checks ───────────────────────────────────────────────────


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(GMAIL_SCRIPT.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_trades_query_reads_every_key_column():
    literals = [n.value for n in ast.walk(_function("get_trades_table")) if isinstance(n, ast.Constant)]
    select = next(s for s in literals if isinstance(s, str) and s.lstrip().upper().startswith("SELECT"))
    columns = {c.strip().lower() for c in select.strip()[len("SELECT") :].split(",")}
    assert set(TRADE_KEY) <= columns


def test_duplicate_check_runs_before_any_database_write():
    calls = sorted(  # source order; ast.walk is breadth-first
        (n.lineno, n.col_offset, n.func.attr)
        for n in ast.walk(_function("run_pipeline"))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    )
    order = {name: i for i, (_, _, name) in enumerate(calls)}
    assert order["get_trades_table"] < order["drop_already_loaded"]  # fresh read before the check
    assert order["resolve_trades_df"] < order["drop_already_loaded"] < order["resolve_assets_with_db"]
    assert order["drop_already_loaded"] < order["load_to_db"]
    # Every nota is proven against its printout first, already loaded or not …
    assert order["extract_fees"] < order["reconcile"] < order["get_trades_table"]
    # … and the one write follows every prompt.
    assert order["load_to_db"] > max(order["resolve_trades_df"], order["resolve_trades_portfolio"], order["resolve_assets_with_db"])


def test_only_load_to_db_writes():
    for name in ("resolve_assets_with_db", "resolve_tickers_with_db", "resolve_portfolio_with_db"):
        calls = {n.func.attr for n in ast.walk(_function(name)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not calls & {"connect", "copy_from", "copy_expert", "commit"}, name
