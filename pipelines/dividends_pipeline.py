# imports
import pandas as pd
import numpy as np
import uuid
from typing import Set
import psycopg2
from dotenv import load_dotenv
import os
from io import StringIO

from ledger_dedupe import DIVIDEND_KEY, LedgerUnavailableError, plan_idempotent_load


load_dotenv()
unresolved_placeholder = "!!!_input_required_!!!"

INCOME_TYPES = ["JUROS", "DIVIDENDOS", "FRAÇÕES", "RENDIMENTOS"]
# "<TYPE> … <TICKER> <word> <QTY>"; FRAÇÕES lines end with the ticker instead.
PER_SHARE_TYPES = ["JUROS", "DIVIDENDOS", "RENDIMENTOS"]

DB_READ_ERRORS = (psycopg2.Error, pd.errors.DatabaseError)
# Operational failures reported by the CLI; anything else is a bug and keeps
# its full traceback.
CLI_ERRORS = (
    psycopg2.Error,
    pd.errors.DatabaseError,
    LedgerUnavailableError,
    OSError,  # statement file missing / unreadable
    ImportError,  # read_excel needs openpyxl
    ValueError,  # unreadable workbook or malformed statement line
    KeyError,
    IndexError,
    EOFError,
)


def parse_statement(raw: pd.DataFrame) -> pd.DataFrame:
    """Income rows from a broker statement sheet.

    Args:
        raw: The sheet as read below its 13-row header. Columns 1, 2, 3, 5, 6
            hold the movement date, clearing date, description, amount and
            running balance; the ledger ends at the first blank movement date.

    Returns:
        One row per income line, in the dividends-table column order, with
        unresolved ids and portfolios.
    """
    file = raw.iloc[:, [1, 2, 3, 5, 6]].copy()
    file.columns = [
        "dividend_date",
        "clearing_date",
        "description",
        "total",
        "balance",
    ]
    blank = file["dividend_date"].isna().to_numpy()
    if blank.any():  # without a blank row argmax() is 0 and nothing would load
        file = file.iloc[: int(blank.argmax())].copy()

    file["dividend_date"] = pd.to_datetime(file["dividend_date"])
    file["clearing_date"] = pd.to_datetime(file["clearing_date"])
    file["dividend_date"] = file["dividend_date"].dt.date
    file["clearing_date"] = file["clearing_date"].dt.date

    file["type"] = file["description"].str.split().str[0]

    filtered = file[file["type"].isin(INCOME_TYPES)].copy()

    # 1. Split the description into a list of words
    description_split = filtered["description"].str.split()

    # 2. Define the condition
    is_standard_type = filtered["type"].isin(PER_SHARE_TYPES)

    # 3. Use np.where (Condition, Value if True, Value if False)
    filtered["ticker"] = np.where(
        is_standard_type,
        description_split.str[-3],  # Standard ticker position
        description_split.str[-1],  # FRAÇÕES ticker position
    )

    filtered["quantity"] = np.where(
        is_standard_type,
        description_split.str[-1],  # Standard quantity position
        "1",  # Default for FRAÇÕES
    )

    filtered["quantity"] = filtered["quantity"].astype("int32")
    filtered["total"] = filtered["total"].astype("float64")
    filtered["dividend"] = (filtered["total"] / filtered["quantity"]).round(2)
    filtered["portfolio"] = unresolved_placeholder
    filtered["dividend_id"] = None

    return filtered[
        [
            "dividend_id",
            "dividend_date",
            "ticker",
            "type",
            "dividend",
            "total",
            "quantity",
            "portfolio",
        ]
    ]

# --- supabase env variables
db_host = os.environ.get("host")
db_name = os.environ.get("dbname")
db_user = os.environ.get("user")
db_password = os.environ.get("password")
db_port = os.environ.get("port")


class DividendsPipeline:
    def __init__(self, file):
        self.file = file

    def run_pipeline(self) -> int:
        """Load the statement's new income rows; return how many were inserted."""
        self.get_trades_db()
        self.get_dividends_db()
        self.transform_file()
        if self.drop_already_loaded() == 0:
            return 0
        self.resolve_ids_diviends_with_db()
        self.resolve_trades_portfolio()
        self.load_new_divs()
        return len(self.df)

    def get_trades_db(self):

        connection = None
        trades_df_db = pd.DataFrame()

        try:
            print("Connecting to database")
            connection = psycopg2.connect(
                host=db_host,
                database=db_name,
                user=db_user,
                password=db_password,
                port=db_port,
            )

            trades_query = "select ticker, portfolio from trades;"
            print("executing query")

            trades_df_db = pd.read_sql_query(trades_query, connection)

        except DB_READ_ERRORS as e:
            print(f"there was a database error: {e}")
            trades_df_db = pd.DataFrame()

        finally:
            if connection:
                connection.close()
                print("closing database connection")

        self.trades_df = trades_df_db

    def get_dividends_db(self):

        connection = None
        div_df_db = pd.DataFrame()

        try:
            print("connecting to database")
            connection = psycopg2.connect(
                host=db_host,
                database=db_name,
                user=db_user,
                password=db_password,
                port=db_port,
            )

            div_query = "select * from dividends;"
            print("executing query")

            div_df_db = pd.read_sql_query(div_query, connection)

        except DB_READ_ERRORS as e:
            print(f"there was a database error: {e}")
            div_df_db = pd.DataFrame()

        finally:
            if connection:
                connection.close()
                print("closing database connection")

        self.div_df = div_df_db

    def transform_file(self):
        self.df = parse_statement(pd.read_excel(self.file, skiprows=13))

    def drop_already_loaded(self) -> int:
        """Drop statement rows the dividends table already holds.

        Statements overlap (re-downloads, wider date ranges) and every run
        mints random ids, so without this a re-run duplicates income.

        Returns:
            Number of rows left to load.
        """
        plan = plan_idempotent_load(self.df, self.div_df, DIVIDEND_KEY)
        print(
            f"{len(plan.skipped)} of {len(self.df)} statement rows already in the "
            f"dividends table — skipped as duplicates; {len(plan.new)} new."
        )
        if not plan.skipped.empty:
            dates = plan.skipped["dividend_date"]
            print(f"   skipped rows span {dates.min()} → {dates.max()}")
        self.df = plan.new
        return len(plan.new)

    def add_validated_dividend_id_for_missing_robust(
        self, df: pd.DataFrame, db_df: pd.DataFrame = None
    ) -> pd.DataFrame:
        """
        generates unique uuids for rows missing 'dividend_id'.
        checks against both the current DataFrame and the existing database records.
        """
        df_copy = df.copy()

        # 1. ensure the 'dividend_id' column exists
        if "dividend_id" not in df_copy.columns:
            df_copy["dividend_id"] = None

        df_copy["dividend_id"] = df_copy["dividend_id"].astype("object")

        # 2. identify missing rows
        # note: handles none, nan, and empty strings
        missing_mask = df_copy["dividend_id"].isna() | (df_copy["dividend_id"] == "")
        num_missing = missing_mask.sum()

        if num_missing == 0:
            return df_copy

        # 3. build the Set of globally existing ids to prevent collisions
        existing_ids: Set[str] = set(df_copy["dividend_id"].dropna().unique())

        if db_df is not None and not db_df.empty and "dividend_id" in db_df.columns:
            db_ids = set(db_df["dividend_id"].dropna().unique())
            existing_ids.update(db_ids)

        print(
            f"generating {num_missing} ids. checking against {len(existing_ids)} total existing ids."
        )

        # 4. generate new unique hex ids
        new_ids = []
        for _ in range(num_missing):
            while True:
                candidate_id = uuid.uuid4().hex
                if candidate_id not in existing_ids:
                    new_ids.append(candidate_id)
                    existing_ids.add(
                        candidate_id
                    )  # add to Set to prevent internal collision
                    break

        # 5. assign to the missing rows
        df_copy.loc[missing_mask, "dividend_id"] = new_ids

        print(f"✅ assigned {num_missing} new ids.")
        return df_copy

    def resolve_ids_diviends_with_db(self):
        self.df = self.add_validated_dividend_id_for_missing_robust(
            self.df, self.div_df
        )

    def resolve_portfolio_with_db(
        self,
        df: pd.DataFrame,
        database_df: pd.DataFrame,
        asset_column: str = "ticker",
        portfolio_column: str = "portfolio",
    ) -> pd.DataFrame:
        """
        1. checks database_df for existing ticker-to-portfolio mappings.
        2. updates the main DataFrame with found mappings.
        3. prompts for manual input only for assets still missing.
        """
        df_copy = self.df.copy()

        # --- step 1: database lookup ---
        # Get unique ticker -> portfolio mappings only
        mapping_dict = (
            database_df[[asset_column, portfolio_column]]
            .drop_duplicates(subset=[asset_column])
            .set_index(asset_column)[portfolio_column]
            .to_dict()
        )

        mask = (df_copy[portfolio_column] == unresolved_placeholder) | df_copy[
            portfolio_column
        ].isna()
        df_copy.loc[mask, portfolio_column] = df_copy.loc[mask, asset_column].map(
            mapping_dict
        )

        # Re-check for what is still unresolved
        unresolved_indices = df_copy[
            (df_copy[portfolio_column] == unresolved_placeholder)
            | df_copy[portfolio_column].isna()
        ].index.tolist()

        if not unresolved_indices:
            print("✅ all portfolios resolved via database.")
            return df_copy

        print(f"--- {len(unresolved_indices)} trades still require manual input ---")

        for idx in unresolved_indices:
            asset_name = df_copy.loc[idx, asset_column]
            try:
                new_portfolio = input(f"[{idx}] enter portfolio for '{asset_name}': ")
                if new_portfolio.strip():
                    df_copy.loc[idx, portfolio_column] = new_portfolio.strip().upper()
                    print(
                        f"✅ assigned '{new_portfolio.strip().upper()}' to '{asset_name}'."
                    )
                else:
                    df_copy.loc[idx, portfolio_column] = unresolved_placeholder
            except (EOFError, OSError, ValueError):  # no usable stdin
                df_copy.loc[idx, portfolio_column] = unresolved_placeholder

        return df_copy

    def resolve_trades_portfolio(self):
        self.df = self.resolve_portfolio_with_db(
            self.df, self.trades_df, "ticker", "portfolio"
        )

    def load_new_divs(self):
        buffer = StringIO()
        self.df.to_csv(buffer, header=False, index=False, sep="|")
        buffer.seek(0)

        columns = self.df.columns.to_list()

        connection = None

        try:
            print("connection to database")
            connection = psycopg2.connect(
                host=db_host,
                database=db_name,
                user=db_user,
                password=db_password,
                port=db_port,
            )
            with connection.cursor() as cur:
                cur.copy_from(file=buffer, table="dividends", sep="|", columns=columns)
                connection.commit()
                print(f"successfully loaded {len(self.df)} rows to dividends table")

        except psycopg2.Error as e:
            if connection:
                connection.rollback()
            print(f"database error: {e}")
            raise  # a failed load must not be reported as a success

        finally:
            if connection:
                connection.close()


if __name__ == "__main__":
    import sys
    import traceback

    try:
        file_path = input("drag and drop dividends file here").strip(" \"'")
        dividend_loader = DividendsPipeline(file_path)
        if dividend_loader.run_pipeline():
            print("Dividends loaded successfully!")
        else:
            print("No new dividends to load.")

    except CLI_ERRORS as e:
        print(f"program error: {e}")
        traceback.print_exc()
        sys.exit(1)  # a failed import must not look like a clean run
