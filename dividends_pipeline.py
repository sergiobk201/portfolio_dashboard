# imports
import pandas as pd
import datetime as dt
import numpy as np
import uuid
from typing import Set
import psycopg2
from dotenv import load_dotenv
import os
from io import StringIO


load_dotenv()
unresolved_placeholder = "!!!_input_required_!!!"

# --- supabase env variables
db_host = os.environ.get("host")
db_name = os.environ.get("dbname")
db_user = os.environ.get("user")
db_password = os.environ.get("password")
db_port = os.environ.get("port")


class DividendsPipeline:
    def __init__(self, file):
        self.file = file

    def run_pipeline(self):
        self.get_trades_db()
        self.get_dividends_db()
        self.transform_file()
        self.resolve_ids_diviends_with_db()
        self.resolve_trades_portfolio()
        self.load_new_divs()

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

        except Exception as e:
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

        except Exception as e:
            print(f"there was a database error: {e}")
            div_df_db = pd.DataFrame()

        finally:
            if connection:
                connection.close()
                print("closing database connection")

        self.div_df = div_df_db

    def transform_file(self):
        file = pd.read_excel(self.file, skiprows=13)
        file = file.iloc[:, [1, 2, 3, 5, 6]]
        file.columns = [
            "dividend_date",
            "clearing_date",
            "description",
            "total",
            "balance",
        ]
        first_null = file["dividend_date"].isna().argmax()
        file = file.iloc[:first_null]

        file["dividend_date"] = pd.to_datetime(file["dividend_date"])
        file["clearing_date"] = pd.to_datetime(file["clearing_date"])
        file["dividend_date"] = file["dividend_date"].dt.date
        file["clearing_date"] = file["clearing_date"].dt.date

        file["type"] = file["description"].str.split().str[0]

        filtered = file.copy()

        filtered = filtered[filtered["type"].isin(["JUROS", "DIVIDENDOS"])]
        filtered["ticker"] = filtered["description"].str.split().str[-3]
        filtered["quantity"] = filtered["description"].str.split().str[-1]

        filtered["quantity"] = filtered["quantity"].astype("int32")
        filtered["total"] = filtered["total"].astype("float64")
        filtered["dividend"] = (filtered["total"] / filtered["quantity"]).round(2)
        filtered["portfolio"] = unresolved_placeholder
        filtered["dividend_id"] = None

        filtered = filtered[
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

        self.df = filtered

    def add_validated_dividend_id_for_missing_robust(
        df: pd.DataFrame, db_df: pd.DataFrame = None
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

        mask = (df_copy[portfolio_column] == unresolved_placeholder) | df_copy[portfolio_column].isna()
        df_copy.loc[mask, portfolio_column] = df_copy.loc[mask, asset_column].map(mapping_dict)

        # Re-check for what is still unresolved
        unresolved_indices = df_copy[
            (df_copy[portfolio_column] == unresolved_placeholder) | df_copy[portfolio_column].isna()
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
            except Exception as e:
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

        except Exception as e:
            print(f"database error: {e}")

        try:
            with connection.cursor() as cur:
                cur.copy_from(file=buffer, table="dividends", sep="|", columns=columns)
                connection.commit()
                print(f"successfully loaded {len(self.df)} rows to dividends table")

        except Exception as e:
            if connection:
                connection.rollback()
            print(f"database error: {e}")

        finally:
            if connection:
                connection.close()

if __name__ == "__main__":
    import sys

    try:
        file_path = input("drag and drop dividends file here").strip(" \"'")
        dividend_loader = DividendsPipeline(file_path)
        dividend_loader.run_pipeline()
        print("Dividends loaded successfully!")

    except Exception as e:
        print(f"program error: {e}")
        import traceback
        traceback.print_exc()

