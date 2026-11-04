"""
gmail_automation.py
───────────────────
Monthly trade import: every Rico nota de corretagem e-mailed in the last N days
(default 45) → `trades`, oldest first.

Each nota runs the full pipeline on fresh state with a fresh database read, so
it sees what the nota before it just loaded, and the duplicate check
(ledger_dedupe) skips notas already in the table: re-runs and overlapping
windows are safe. The batch stops at the first nota that fails.

Usage: python pipelines/gmail_automation.py [--days N] [--keep-pdfs]
"""

from __future__ import annotations

### Imports
import argparse
import base64
import os
import datetime
import sys
import traceback
from dataclasses import dataclass
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import Error as GoogleApiError
import httplib2
import pikepdf
from pathlib import Path
import pdfplumber
import pandas as pd
import numpy as np
from typing import Any, Sequence, Union
import re
import psycopg2
import uuid
from typing import Set
from io import StringIO
from dotenv import load_dotenv
import yfinance as yf
from google.auth.exceptions import GoogleAuthError, RefreshError

from ledger_dedupe import TRADE_KEY, LedgerUnavailableError, plan_idempotent_load
from nota_fees import cblc_fees, extract_dc_flag

try:  # yfinance ≥ 0.2.50 exposes a typed exception hierarchy
    from yfinance.exceptions import YFException
except ImportError:  # pragma: no cover - older yfinance
    YFException = RuntimeError  # type: ignore[misc,assignment]

# Gmail API, OAuth transport, network and attachment-write failures.
GMAIL_ERRORS = (GoogleApiError, GoogleAuthError, httplib2.HttpLib2Error, OSError, KeyError, ValueError)
DB_READ_ERRORS = (psycopg2.Error, pd.errors.DatabaseError)
# Sector/industry lookup is cosmetic: any provider failure falls back to "Unknown".
YF_INFO_ERRORS = (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, YFException)
# Operational failures: a nota hitting one is reported as FAILED and stops the
# batch; anything else is a bug and keeps its full traceback.
CLI_ERRORS = (
    psycopg2.Error,
    pd.errors.DatabaseError,
    LedgerUnavailableError,
    pikepdf.PasswordError,  # wrong PASSWORD_PDF
    pikepdf.PdfError,  # corrupt attachment
    *GMAIL_ERRORS,
    IndexError,
    EOFError,
)

# --- Path resolution
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ATTACHMENTS_DIR = PROJECT_ROOT / "attachments"  # git-ignored

load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
pdf_password = os.getenv("PASSWORD_PDF")
UNRESOLVED_PLACEHOLDER = "!!!_INPUT_REQUIRED_!!!"

# --- Supabase env variables
DB_HOST = os.environ.get("host")
DB_NAME = os.environ.get("dbname")
DB_USER = os.environ.get("user")
DB_PASSWORD = os.environ.get("password")
DB_PORT = os.environ.get("port")

# --- Gmail window
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]  # changing scopes → delete token.json
RICO_SENDER = "noreply@rico.com.vc"  # the sender may change: verify and add more if needed
DEFAULT_WINDOW_DAYS = 45  # a month plus slack, so a late monthly run still overlaps the last one

# --- Per-nota outcomes
LOADED = "LOADED"
ALREADY_LOADED = "ALREADY_LOADED"
NO_TRADES = "NO_TRADES"
NO_PDF = "NO_PDF"
FAILED = "FAILED"


@dataclass(frozen=True)
class NotaResult:
    """Outcome of one nota PDF, for the run summary (counts only, never amounts).

    Attributes:
        message_id: Gmail message the PDF came from.
        received: Receipt day of the e-mail (ISO, São Paulo time).
        trade_date: Trading day printed on the nota (ISO); empty if not read.
        status: LOADED, ALREADY_LOADED, NO_TRADES, NO_PDF or FAILED.
        inserted: Trade rows inserted.
        skipped: Nota lines skipped as already in the trades table.
        error: Failure reason when status is FAILED.
    """

    message_id: str
    received: str
    trade_date: str = ""
    status: str = LOADED
    inserted: int = 0
    skipped: int = 0
    error: str = ""


def gmail_service() -> Any:
    """Authorised Gmail API client (read-only scope).

    Reuses token.json, refreshes it when expired, and falls back to the
    browser consent flow with credentials.json (pipelines/ or project root).

    Returns:
        A googleapiclient Gmail v1 resource.

    Raises:
        FileNotFoundError: No usable token and no credentials.json.
    """
    creds = None

    token_path = SCRIPT_DIR / "token.json"
    credentials_path = SCRIPT_DIR / "credentials.json"

    # Check project root if credentials.json is not in pipelines/
    if not credentials_path.exists():
        root_credentials = PROJECT_ROOT / "credentials.json"
        if root_credentials.exists():
            credentials_path = root_credentials

    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        except (OSError, ValueError) as e:  # unreadable or malformed → re-authenticate
            print(f"Error loading token.json: {e}")
            creds = None

    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                print(f"Refresh token expired or invalid, forcing re-authentication: {e}")
                creds = None  # Force re-authentication flow

        if not creds:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Google OAuth client secrets file 'credentials.json' not found at {credentials_path}.\n"
                    f"Please download it from the Google Cloud Console (OAuth 2.0 Client IDs -> Desktop client) "
                    f"and save it to that location to authenticate."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def nota_query(days: int) -> str:
    """Gmail search for the Rico notas received in the last ``days`` days."""
    return f"from: {RICO_SENDER} has:attachment newer_than:{days}d"


def list_nota_messages(service: Any, query: str) -> list[dict]:
    """Every message matching ``query``, across all result pages, oldest first.

    Gmail returns ids newest first, 100 per page; the order used here comes
    from each message's ``internalDate`` (receipt time, epoch ms) instead.

    Args:
        service: Gmail v1 resource.
        query: Gmail search string.

    Returns:
        Full message resources (payload included), oldest first.
    """
    ids: list[str] = []
    page_token: str | None = None
    while True:
        request = {"userId": "me", "q": query}
        if page_token:
            request["pageToken"] = page_token
        response = service.users().messages().list(**request).execute()
        ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    messages = [
        service.users().messages().get(userId="me", id=message_id, format="full").execute()
        for message_id in dict.fromkeys(ids)  # de-duplicated, order kept
    ]
    return sorted(messages, key=lambda m: (int(m["internalDate"]), m["id"]))


def received_day(message: dict) -> str:
    """Receipt day of a message (ISO, São Paulo time) from its ``internalDate``."""
    received = pd.Timestamp(int(message["internalDate"]), unit="ms", tz="UTC")
    return received.tz_convert("America/Sao_Paulo").date().isoformat()


def pdf_parts(part: dict) -> list[dict]:
    """PDF attachment parts of a message payload, in MIME order."""
    if part.get("parts"):
        return [pdf for child in part["parts"] for pdf in pdf_parts(child)]
    filename = part.get("filename") or ""
    body = part.get("body", {})
    is_pdf = part.get("mimeType") == "application/pdf" or filename.lower().endswith(".pdf")
    return [part] if filename and is_pdf and (body.get("attachmentId") or body.get("data")) else []


def save_nota_pdfs(service: Any, message: dict, attachments_dir: Path) -> list[Path]:
    """Download one message's PDF attachments to per-message files.

    Each file is named after the message id and rewritten on every run, and
    only the paths returned here are processed — a file left by an earlier run
    is never picked up in place of a failed download.

    Args:
        service: Gmail v1 resource.
        message: Full message resource.
        attachments_dir: Download folder.

    Returns:
        Paths written by this call; empty when the e-mail has no PDF.

    Raises:
        GoogleApiError, OSError, …: Download or write failure (GMAIL_ERRORS);
            the files this call already wrote are removed.
    """
    attachments_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for n, part in enumerate(pdf_parts(message["payload"]), start=1):
            body = part["body"]
            data = body.get("data") or (  # small attachments come inline
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message["id"], id=body["attachmentId"])
                .execute()["data"]
            )
            path = attachments_dir / f"trade_confirmation_{message['id']}_{n}.pdf"
            path.write_bytes(base64.urlsafe_b64decode(data))
            written.append(path)
    except GMAIL_ERRORS:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return written


def pdf_text(path: Path, max_pages: int = 5) -> str:
    """Text of a PDF's first pages (a nota spans one or two)."""
    with pdfplumber.open(str(path)) as pdf:
        pages = (page.extract_text() for page in pdf.pages[:max_pages])
        return "".join(f"{text}\n" for text in pages if text)  # image-only pages yield None


TRADE_LINE = re.compile(r"^1-BOVESPA.*$", flags=re.MULTILINE)
NOTA_COLUMNS = ["market", "side", "type", "asset", "quantity", "price", "total"]
# Share count: plain digits, or digits with a thousands dot ("1.000").
QUANTITY_TOKEN = re.compile(r"\d{1,3}(?:\.\d{3})+|\d+")


def br_number(token: str) -> float:
    """Brazilian-format number → float ('1.234,56' → 1234.56, '34,17' → 34.17).

    Raises:
        ValueError: The token is not a number.
    """
    return float(token.replace(".", "").replace(",", "."))


def parse_trade_lines(text: str) -> pd.DataFrame:
    """'1-BOVESPA' lines of a nota → one row per trade (Brazilian number format).

    Each line must reconcile on its own: quantity × price = total, within the
    rounding of a price printed to the cent. A line read with the wrong anchor
    (a new layout, a termo or options line) stops the nota instead of loading
    a wrong trade into the tax ledger.

    Args:
        text: Nota text.

    Returns:
        NOTA_COLUMNS frame; empty when the text has no Bovespa lines.

    Raises:
        ValueError: A line has no quantity / price / total columns, or its
            numbers do not reconcile.
    """
    records = []
    for n, line in enumerate(TRADE_LINE.findall(text), start=1):
        parts = line.split()

        market = parts[0]  # 1-BOVESPA
        side = parts[1]  # C or V
        tipo = parts[2]  # FRACIONARIO, VISTA, etc.

        # Quantity is the first integer token after the description and its
        # observation marker (e.g. '@'); price and total follow it.
        index = next((i for i in range(3, len(parts)) if QUANTITY_TOKEN.fullmatch(parts[i])), None)
        if index is None or index + 2 >= len(parts):
            raise ValueError(f"trade line {n}: no quantity / price / total columns — nota layout changed?")
        description = " ".join(parts[3 : index - 1])

        quantity = int(parts[index].replace(".", ""))
        raw_price = br_number(parts[index + 1])
        total = round(br_number(parts[index + 2]), 2)
        # Tolerance: a per-fill average price printed to the cent, plus the cent on the total.
        if abs(quantity * raw_price - total) > 0.005 * quantity + 0.01:
            raise ValueError(f"trade line {n}: quantity × price ≠ total — misread line or new nota layout")
        price = round(raw_price, 2)

        records.append(
            {
                "market": market,
                "side": side,
                "type": tipo,
                "asset": description,
                "quantity": quantity,
                "price": price,
                "total": total,
            }
        )
    return pd.DataFrame(records, columns=NOTA_COLUMNS)


def iso_day(text: str | None) -> str:
    """dd/mm/yyyy → ISO date; anything unparseable is returned as is."""
    try:
        return datetime.datetime.strptime(text, "%d/%m/%Y").date().isoformat()
    except (TypeError, ValueError):
        return text or ""


class GmailRicoImporter:
    """Loads ONE downloaded nota PDF into `trades`; use a fresh instance per nota."""

    def __init__(self, pdf_path: Union[str, Path], keep_pdfs: bool = False) -> None:
        self.pdf_path = Path(pdf_path)
        stem = self.pdf_path.stem.removeprefix("trade_confirmation_")
        self.unlocked_path = self.pdf_path.with_name(f"statement_unlocked_{stem}.pdf")
        self.keep_pdfs = keep_pdfs
        self.df = pd.DataFrame()
        self.full_text = ""
        self.trade_date: str | None = None
        self.lines = 0  # Bovespa lines on the nota
        self.skipped = 0  # of those, already in the trades table
        self.inserted = 0
        self.db_trades_df = pd.DataFrame()
        self.db_assets_df = pd.DataFrame()

    def run_pipeline(self) -> int:
        """Load this nota's new trades; return how many rows were inserted.

        The encrypted and decrypted PDFs are deleted afterwards, also on
        failure, unless ``keep_pdfs``.
        """
        try:
            self.final_pdf()
            self.read_pdf()
            if self.df.empty:
                print("⚠️ No '1-BOVESPA' lines — not a Bovespa nota, or the layout changed. Nothing loaded.")
                return 0
            self.extract_fees()
            self.add_data_to_df()
            self.get_trades_table()  # fresh read: includes the nota loaded just before this one
            self.get_assets_table()

            self.resolve_trades_df()
            new_lines = self.drop_already_loaded()
            self.skipped = self.lines - new_lines
            if new_lines == 0:
                return 0
            self.resolve_assets_with_db()

            self.resolve_trades_portfolio()
            self.validate_trade_ids()
            self.final_transformation()

            self.load_to_db()
            self.inserted = len(self.df)
            print("--- Nota loaded")
            return self.inserted
        finally:
            self.discard_pdfs()

    def result(self, message_id: str, received: str, error: str = "") -> NotaResult:
        """This nota's outcome for the run summary."""
        if error:
            status = FAILED
        elif self.lines == 0:
            status = NO_TRADES
        else:
            status = LOADED if self.inserted else ALREADY_LOADED
        return NotaResult(message_id, received, iso_day(self.trade_date), status, self.inserted, self.skipped, error)

    def discard_pdfs(self) -> None:
        """Delete this nota's encrypted and decrypted PDFs (personal data) unless kept."""
        if self.keep_pdfs:
            return
        for path in (self.pdf_path, self.unlocked_path):
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                print(f"Could not delete {path.name}: {e}")

    def decrypt_pdf(self, input_path, output_path, password):
        """Open password-protected PDF and save unencrypted copy."""
        with pikepdf.open(input_path, password=password) as pdf:
            pdf.save(output_path)

    def final_pdf(self) -> None:
        """Decrypt the downloaded nota next to it (PASSWORD_PDF).

        Raises:
            pikepdf.PasswordError: PASSWORD_PDF does not open this PDF.
        """
        try:
            self.decrypt_pdf(self.pdf_path, self.unlocked_path, pdf_password or "")
        except pikepdf.PasswordError as e:
            raise pikepdf.PasswordError(
                f"{self.pdf_path.name}: PASSWORD_PDF (.env) does not open it — wrong password, or not a nota"
            ) from e
        print("Decrypted PDF saved:", self.unlocked_path.name)

    def read_pdf(self) -> None:
        """Extract the decrypted nota's text, trading day and Bovespa lines."""
        self.full_text = pdf_text(self.unlocked_path)
        self.trade_date = self.extract_first_date(self.full_text)  # named in failure reports
        self.df = parse_trade_lines(self.full_text)
        self.lines = len(self.df)

    def extract_single_value(self, label, text):
        pattern = rf"{label}\s+([\d\.,]+)"
        match = re.search(pattern, text)
        return match.group(1) if match else None


    def extract_fees(self):

        total_cblc = self.extract_single_value("Total CBLC", self.full_text)
        self.cblc_flag = extract_dc_flag("Total CBLC", self.full_text)
        print("Total CBLC:", total_cblc, self.cblc_flag or "(no D/C flag)")

        total_bovespa = self.extract_single_value(
            "Total Bovespa / Soma", self.full_text
        )
        print("Total Bovespa", total_bovespa)

        total_custos = self.extract_single_value(
            "Total Custos / Despesas", self.full_text
        )
        print("Total Custos", total_custos)

        labels = {"Total CBLC": total_cblc, "Total Bovespa / Soma": total_bovespa, "Total Custos / Despesas": total_custos}
        missing = [label for label, value in labels.items() if value is None]
        if missing:
            raise ValueError(f"nota text has no {missing} — layout changed?")

        total_cblc = round(float(total_cblc.replace(".", "").replace(",", ".")), 2)
        total_bovespa = round(
            float(total_bovespa.replace(".", "").replace(",", ".")), 2
        )
        total_custos = round(float(total_custos.replace(".", "").replace(",", ".")), 2)

        self.total_cblc = total_cblc
        self.total_bovespa = total_bovespa
        self.total_custos = total_custos

    def extract_first_date(self, text):
        pattern = r"\b(\d{2}/\d{2}/\d{4})\b"
        match = re.search(pattern, text)
        return match.group(1) if match else None

    def add_data_to_df(self):

        trade_date = self.extract_first_date(self.full_text)
        if trade_date is None:
            raise ValueError("no trading day (dd/mm/yyyy) in the nota text")
        self.trade_date = trade_date

        total_trade_volume = self.df["total"].sum()
        buys = float(self.df.loc[self.df["side"] == "C", "total"].sum())
        sells = float(self.df.loc[self.df["side"] == "V", "total"].sum())
        total_fees = round(
            cblc_fees(self.total_cblc, self.cblc_flag, buys, sells) + self.total_custos + self.total_bovespa,
            2,
        )

        # Pro-rata by trade value: each trade carries its share of the note's costs.
        self.df["fee"] = round(self.df["total"] / total_trade_volume * total_fees, 2)
        self.df["ticker"] = None

    def get_trades_table(self):

        TARGET_TABLE = "trades"

        connection = None
        db_trades_df = pd.DataFrame()  # Initialize an empty DataFrame

        try:
            # 1. Establish the connection
            print(f"Connecting to the '{DB_NAME}' database...")
            connection = psycopg2.connect(
                host=DB_HOST,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                port=DB_PORT,
            )

            self.connection = connection

            # 2. Define the SQL query (date…total feed the re-run duplicate check)
            sql_query = (
                "SELECT trade_id, date, side, asset, ticker, quantity, total, portfolio "
                f"FROM {TARGET_TABLE};"
            )

            # 3. Use pandas to execute the query and read directly into a DataFrame
            print(f"Executing query: {sql_query}")
            db_trades_df = pd.read_sql_query(
                sql_query,
                connection,  # pandas handles the cursor and fetching internally
            )

            # 4. Success message and data inspection
            print("\n--- ✅ Data Extraction Successful ---")
            print(f"DataFrame shape: {db_trades_df.shape}")
            print("\n--- DataFrame Head ---")
            print(db_trades_df.head())

        except DB_READ_ERRORS as e:
            print(f"❌ Database connection or query error: {e}")
            raise  # an unreadable ledger stops this nota; it must never look empty

        finally:
            # Always close the connection
            if connection:
                connection.close()
                print("\nDatabase connection closed.")

        self.db_trades_df = db_trades_df

    def get_assets_table(self):

        TARGET_TABLE = "assets"

        connection = None
        db_assets_df = pd.DataFrame()  # Initialize an empty DataFrame

        try:
            # 1. Establish the connection
            print(f"Connecting to the '{DB_NAME}' database...")
            connection = psycopg2.connect(
                host=DB_HOST,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                port=DB_PORT,
            )

            self.connection = connection

            # 2. Define the SQL query
            sql_query = f"SELECT ticker, company_name FROM {TARGET_TABLE};"

            # 3. Use pandas to execute the query and read directly into a DataFrame
            print(f"Executing query: {sql_query}")
            db_assets_df = pd.read_sql_query(
                sql_query,
                connection,  # pandas handles the cursor and fetching internally
            )

            # 4. Success message and data inspection
            print("\n--- ✅ Data Extraction Successful ---")
            print(f"DataFrame shape: {db_assets_df.shape}")
            print("\n--- DataFrame Head ---")
            print(db_assets_df.head())

        except DB_READ_ERRORS as e:
            print(f"❌ Database connection or query error: {e}")
            raise  # an unreadable assets table stops this nota; it must never look empty

        finally:
            # Always close the connection
            if connection:
                connection.close()
                print("\nDatabase connection closed.")

        self.db_assets_df = db_assets_df

    def resolve_tickers_with_db(
        self,
        df: pd.DataFrame,
        database_df: pd.DataFrame,
        asset_column: str = "asset",
        ticker_column: str = "ticker",
    ) -> pd.DataFrame:
        """
        Resolves tickers using a hierarchy:
        1. Database mapping
        2. Internal DataFrame consistency
        3. Manual user input
        """
        df_copy = df.copy()

        # Identify unique asset names that need a ticker
        unresolved_assets = df_copy[
            (df_copy[ticker_column] == UNRESOLVED_PLACEHOLDER)
            | df_copy[ticker_column].isna()
        ][asset_column].unique()

        if len(unresolved_assets) == 0:
            print("✅ No unresolved tickers found.")
            return df_copy

        # Create a mapping dictionary from the DB for O(1) lookup
        db_mapping = database_df.set_index(asset_column)[ticker_column].to_dict()

        assets_requiring_manual_input = []

        print(f"--- Resolving {len(unresolved_assets)} Assets ---")

        for asset_name in unresolved_assets:
            # --- PRIORITY 1: DATABASE LOOKUP ---
            if asset_name in db_mapping:
                resolved_ticker = db_mapping[asset_name]
                df_copy.loc[df_copy[asset_column] == asset_name, ticker_column] = (
                    resolved_ticker
                )
                print(f"🗄️ DB-resolved '{asset_name}' -> '{resolved_ticker}'.")
                continue

            # --- PRIORITY 2: INTERNAL LOOKUP (Check current DF for existing values) ---
            internal_check = df_copy[
                (df_copy[asset_column] == asset_name)
                & (df_copy[ticker_column] != UNRESOLVED_PLACEHOLDER)
                & df_copy[ticker_column].notna()
            ][ticker_column].unique()

            if len(internal_check) >= 1:
                resolved_ticker = internal_check[0]
                df_copy.loc[df_copy[asset_column] == asset_name, ticker_column] = (
                    resolved_ticker
                )
                print(f"🔄 Internally resolved '{asset_name}' -> '{resolved_ticker}'.")

            else:
                # --- PRIORITY 3: ADD TO MANUAL LIST ---
                assets_requiring_manual_input.append(asset_name)

        # --- STEP 3: MANUAL INPUT ---
        if assets_requiring_manual_input:
            print("\n--- MANUAL TICKER INPUT REQUIRED ---")
            for asset in assets_requiring_manual_input:
                try:
                    new_ticker = input(f"Enter ticker for '{asset}': ")
                    if new_ticker.strip():
                        ticker_val = new_ticker.strip().upper()
                        df_copy.loc[df_copy[asset_column] == asset, ticker_column] = (
                            ticker_val
                        )
                        print(f"✅ Ticker '{ticker_val}' assigned to '{asset}'.")
                    else:
                        df_copy.loc[df_copy[asset_column] == asset, ticker_column] = (
                            UNRESOLVED_PLACEHOLDER
                        )
                except EOFError:
                    df_copy.loc[df_copy[asset_column] == asset, ticker_column] = (
                        UNRESOLVED_PLACEHOLDER
                    )

        print("----------------------------------\n")
        return df_copy

    def resolve_trades_df(self):
        self.df = self.resolve_tickers_with_db(
            self.df, self.db_trades_df, "asset", "ticker"
        )

        format_date = "%d/%m/%Y"

        date_object = datetime.datetime.strptime(self.trade_date, format_date)
        self.df["date"] = date_object.date()

        new_order = [
            "date",
            "side",
            "ticker",
            "asset",
            "quantity",
            "price",
            "total",
            "fee",
        ]

        # 1. Define the mapping
        mapping = {"C": "BUY", "V": "SELL"}

        # 2. Apply the mapping to the specific column
        #    (The 'inplace=True' changes the DataFrame directly)
        self.df["side"] = self.df["side"].replace(mapping)

        self.df = self.df[new_order]
        self.df["portfolio"] = None

    def drop_already_loaded(self) -> int:
        """Drop nota lines the trades table already holds.

        Re-runs, overlapping Gmail windows and re-sent notas all bring back
        notas already loaded, and trade ids are random, so without this check
        the same trades would be inserted again.

        Returns:
            Number of lines left to load.
        """
        plan = plan_idempotent_load(self.df, self.db_trades_df, TRADE_KEY)
        print(
            f"{len(plan.skipped)} of {len(self.df)} lines of nota {self.trade_date} already in the "
            f"trades table — skipped as duplicates; {len(plan.new)} new."
        )
        if not plan.skipped.empty and not plan.new.empty:
            # A nota loads all-or-nothing, so a partial match deserves a look.
            print("⚠️ Partial match — verify these skipped lines are really in the table:")
            for row in plan.skipped.itertuples(index=False):
                print(f"   {row.side} {row.quantity} {row.ticker}")
        self.df = plan.new
        return len(plan.new)

    def resolve_portfolio_with_db(
        self,
        df: pd.DataFrame,
        database_df: pd.DataFrame,
        asset_column: str = "asset",
        portfolio_column: str = "portfolio",
    ) -> pd.DataFrame:
        """
        1. Checks database_df for existing ticker-to-portfolio mappings.
        2. Updates the main DataFrame with found mappings.
        3. Prompts for manual input ONLY for assets still missing.
        """
        df_copy = df.copy()

        # --- STEP 1: DATABASE LOOKUP ---
        # Ensure database_df is cleaned for mapping
        mapping_dict = database_df.set_index(asset_column)[portfolio_column].to_dict()

        # Apply mapping where the portfolio is currently missing or placeholder
        mask = (df_copy[portfolio_column] == UNRESOLVED_PLACEHOLDER) | df_copy[
            portfolio_column
        ].isna()

        # Map the assets to known portfolios
        df_copy.loc[mask, portfolio_column] = df_copy.loc[mask, asset_column].map(
            mapping_dict
        )

        # --- STEP 2: MANUAL INPUT FOR REMAINING ---
        # Re-evaluate unresolved indices after DB update
        unresolved_indices = df_copy[
            (df_copy[portfolio_column] == UNRESOLVED_PLACEHOLDER)
            | df_copy[portfolio_column].isna()
        ].index.tolist()

        if not unresolved_indices:
            print("✅ All portfolios resolved via Database.")
            return df_copy

        print(f"--- {len(unresolved_indices)} Trades still require MANUAL INPUT ---")

        for idx in unresolved_indices:
            asset_name = df_copy.loc[idx, asset_column]
            try:
                new_portfolio = input(f"[{idx}] Enter Portfolio for '{asset_name}': ")
                if new_portfolio.strip():
                    df_copy.loc[idx, portfolio_column] = new_portfolio.strip().upper()
                    print(
                        f"✅ Assigned '{new_portfolio.strip().upper()}' to '{asset_name}'."
                    )
                else:
                    df_copy.loc[idx, portfolio_column] = UNRESOLVED_PLACEHOLDER
            except EOFError:
                df_copy.loc[idx, portfolio_column] = UNRESOLVED_PLACEHOLDER

        return df_copy

    def resolve_trades_portfolio(self):
        self.df = self.resolve_portfolio_with_db(
            self.df, self.db_trades_df, "ticker", "portfolio"
        )

    def add_validated_trade_id_for_missing_robust(
        self, df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Generates unique UUIDs only for rows where 'trade_id' is missing (NaN/None),
        ensuring no collision and explicitly handling data types to prevent warnings.
        """
        df_copy = df.copy()
        trades_df_copy = self.db_trades_df

        # 1. Ensure the 'trade_id' column exists, using 'object' dtype
        if "trade_id" not in df_copy.columns:
            # Create the column and immediately set its type to 'object' (string)
            df_copy["trade_id"] = pd.Series([np.nan] * len(df_copy), dtype="object")
            print("💡 'trade_id' column created as type 'object'.")
        else:
            # If it exists, cast it to 'object' to handle any prior float/mixed inference
            trades_df_copy["trade_id"] = trades_df_copy["trade_id"].astype("object")

        # 2. Identify the rows where 'trade_id' is missing
        missing_mask = df_copy["trade_id"].isna()
        num_missing = missing_mask.sum()

        if num_missing == 0:
            print(
                "✅ No missing 'trade_id' values found. DataFrame returned unchanged."
            )
            return df_copy

        # 3. Create a set of all currently existing, VALID IDs for collision check
        # Note: We can safely use .astype(str) here, as all IDs are non-NaN strings now.
        existing_ids: Set[str] = set(trades_df_copy["trade_id"].dropna())

        print(
            f"Starting ID generation for {num_missing} missing rows. {len(existing_ids)} existing IDs found."
        )

        # 4. Generate new IDs
        new_ids = []
        for _ in range(num_missing):
            unique_id = None
            while True:
                candidate_id = uuid.uuid4().hex
                if candidate_id not in existing_ids:
                    unique_id = candidate_id
                    existing_ids.add(unique_id)
                    break
            new_ids.append(unique_id)

        # 5. Apply the new IDs only to the missing rows
        df_copy.loc[missing_mask, "trade_id"] = new_ids

        print(
            f"✅ Successfully generated and assigned {num_missing} new, non-conflicting IDs."
        )
        return df_copy

    def validate_trade_ids(self):
        self.df = self.add_validated_trade_id_for_missing_robust(self.df)

    def final_transformation(self):
        final_order = [
            "trade_id",
            "date",
            "side",
            "ticker",
            "asset",
            "quantity",
            "price",
            "total",
            "fee",
            "portfolio",
        ]
        self.df = self.df[final_order]

    def resolve_assets_with_db(self):
        print("--- Checking assets table ---")

        current_tickers = set(self.df["ticker"].unique())

        existing_tickers = set(self.db_assets_df["ticker"].unique())

        missing_tickers = current_tickers - existing_tickers

        if not missing_tickers:
            print("All tickers already exist in assets table")

        print(f"Found {len(missing_tickers)} new tickers to register")

        new_assets = []

        for ticker in missing_tickers:
            yf_ticker = f"{ticker}.SA" if not ticker.endswith(".SA") else ticker
            print(f"Fetching data for {yf_ticker}")

            company_name = self.df[self.df["ticker"] == ticker]["asset"].iloc[0]
            industry = "Unknown"
            sector = "Unknown"

            try:
                stock = yf.Ticker(yf_ticker)
                info = stock.info
                industry = info.get("industry", "Unknown")
                sector = info.get("sector", "Unknown")
            except YF_INFO_ERRORS as e:
                print(f"Could not pull yfinance data for {ticker}: {e}")

            new_assets.append(
                {
                    "ticker": ticker,
                    "company_name": company_name,
                    "sector": sector,
                    "industry": industry,
                }
            )

        if new_assets:
            new_assets_df = pd.DataFrame(new_assets)
            buffer_asset = StringIO()
            new_assets_df.to_csv(buffer_asset, header=False, index=False)
            buffer_asset.seek(0)

            assets_columns = new_assets_df.columns.to_list()

            connection = None
            try:
                connection = psycopg2.connect(
                    host=DB_HOST,
                    database=DB_NAME,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    port=DB_PORT,
                )

                with connection.cursor() as cur:
                    cur.copy_from(
                        file=buffer_asset,
                        table="assets",
                        sep=",",
                        columns=assets_columns,
                    )

                    connection.commit()
                    print(
                        f"Successfully inserted {len(new_assets_df)} rows into assets table"
                    )

            except psycopg2.Error as e:
                if connection:
                    connection.rollback()
                print(f"Database error: {e}")

            finally:
                if connection:
                    connection.close()

    def load_to_db(self):
        buffer = StringIO()
        self.df.to_csv(buffer, header=False, index=False)

        buffer.seek(0)

        columns = self.df.columns.to_list()
        connection = None
        try:
            # 1. Establish the connection
            connection = psycopg2.connect(
                host=DB_HOST,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                port=DB_PORT,
            )

            with connection.cursor() as cur:
                cur.copy_from(file=buffer, table="trades", sep=",", columns=columns)

                connection.commit()
                print(f"Successfully inserted {len(self.df)} rows into trades table")

        except psycopg2.Error as e:
            if connection:
                connection.rollback()
            print(f"Database error: {e}")
            raise  # a failed load must not be reported as a success

        finally:
            if connection:
                connection.close()


def import_notas(
    service: Any,
    days: int = DEFAULT_WINDOW_DAYS,
    keep_pdfs: bool = False,
    attachments_dir: Path | None = None,
) -> list[NotaResult]:
    """Import every nota in the Gmail window, oldest first.

    Each PDF gets a fresh importer — fresh state and a fresh database read —
    so the duplicate check sees everything loaded before it. The batch stops
    at the first nota that fails; a re-run skips the notas already loaded.

    Args:
        service: Gmail v1 resource.
        days: Look-back window in days.
        keep_pdfs: Keep the downloaded and decrypted PDFs.
        attachments_dir: Download folder (default: <project>/attachments).

    Returns:
        One result per nota PDF (or per e-mail without one); a FAILED result,
        if any, is the last.
    """
    folder = attachments_dir or ATTACHMENTS_DIR
    messages = list_nota_messages(service, nota_query(days))
    print(f"{len(messages)} Rico e-mail(s) with attachments in the last {days} days — importing oldest first.")
    results: list[NotaResult] = []
    for message in messages:
        received = received_day(message)
        print(f"\n=== Nota e-mail {message['id']} (received {received}) ===")
        try:
            pdfs = save_nota_pdfs(service, message, folder)
        except GMAIL_ERRORS as e:
            results.append(NotaResult(message["id"], received, status=FAILED, error=f"download failed: {e}"))
            return results
        if not pdfs:
            print("No PDF attachment — skipped.")
            results.append(NotaResult(message["id"], received, status=NO_PDF))
        for i, pdf in enumerate(pdfs):
            importer = GmailRicoImporter(pdf, keep_pdfs=keep_pdfs)
            try:
                importer.run_pipeline()
            except CLI_ERRORS as e:
                results.append(importer.result(message["id"], received, error=f"{type(e).__name__}: {e}"))
                if not keep_pdfs:  # this e-mail's other PDFs will not be processed
                    for rest in pdfs[i + 1 :]:
                        rest.unlink(missing_ok=True)
                return results
            results.append(importer.result(message["id"], received))
    return results


def summary_lines(results: list[NotaResult]) -> list[str]:
    """One line per nota (days, e-mail, status, counts) plus a total — counts only."""
    lines = [
        f"nota {r.trade_date or '----------'}  received {r.received}  {r.message_id}  "
        f"{r.status:<14}  inserted {r.inserted:>3}  skipped {r.skipped:>3}"
        for r in results
    ]
    lines.append(
        f"TOTAL {len(results)} nota(s): inserted {sum(r.inserted for r in results)}, "
        f"skipped {sum(r.skipped for r in results)} (already loaded)"
    )
    return lines


def positive_days(text: str) -> int:
    """argparse type for --days: a whole number of days ≥ 1."""
    days = int(text)  # ValueError → argparse reports an invalid value
    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return days


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Command-line options of the monthly trade import."""
    parser = argparse.ArgumentParser(
        description=(
            "Import every Rico nota de corretagem e-mailed in the last N days into the trades "
            "table, oldest first. Notas already loaded are skipped, so re-runs are safe."
        ),
    )
    parser.add_argument(
        "--days",
        type=positive_days,
        default=DEFAULT_WINDOW_DAYS,
        metavar="N",
        help="Gmail look-back window in days (default: %(default)s)",
    )
    parser.add_argument(
        "--keep-pdfs",
        action="store_true",
        help="keep the downloaded and decrypted PDFs in attachments/ (deleted by default: personal data)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the exit code (1 when a nota failed)."""
    args = parse_args(argv)
    print("--- Starting pipeline ---")
    results = import_notas(gmail_service(), days=args.days, keep_pdfs=args.keep_pdfs)

    print("\n--- Summary ---")
    for line in summary_lines(results):
        print(line)

    failed = next((r for r in results if r.status == FAILED), None)
    if failed:
        print(
            f"\n✖ Stopped at nota {failed.trade_date or '(trading day not read)'} — e-mail "
            f"{failed.message_id}, received {failed.received}: {failed.error}"
        )
        print("  Notas before it are loaded. Fix the cause and re-run: loaded notas are skipped.")
        return 1
    print("Trade statement import successful!" if any(r.inserted for r in results) else "No new trades to import.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CLI_ERRORS as e:  # Gmail authorisation or listing failed before any nota
        print(f"Program error: {e}")
        traceback.print_exc()
        sys.exit(1)
