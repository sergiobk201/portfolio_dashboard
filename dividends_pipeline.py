# Imports
import pandas as pd
import datetime as dt
import numpy as np
import uuid
from typing import Set
import psycopg2
from dotenv import load_dotenv
import os
import yfinance as yf
from io import StringIO



load_dotenv()
UNRESOLVED_PLACEHOLDER = '!!!_INPUT_REQUIRED_!!!'

# --- Supabase env variables
DB_HOST = os.environ.get("host")
DB_NAME = os.environ.get("dbname")
DB_USER = os.environ.get("user")
DB_PASSWORD = os.environ.get("password")
DB_PORT = os.environ.get("port")

class DividendsPipeline():

    def __init__(self, file):
        self.file = file

    def run_pipeline(self):
        self.get_trades_db()
        self.get_dividends_db()      
        self.transform_file()     
        self.df = self.add_validated_dividend_id_for_missing_robust(self.df)
        self.resolve_trades_portfolio()
        self.load_new_divs()

    def get_trades_db(self):

        connection = None 
        trades_df_db = pd.DataFrame()

        try:
            print('Connecting to database')
            connection = psycopg2.connect(
                host = DB_HOST, 
                database = DB_NAME, 
                user = DB_USER, 
                password = DB_PASSWORD, 
                port = DB_PORT, 
            )

            trades_query = 'SELECT ticker, portfolio FROM trades;'
            print('Executing query')

            trades_df_db = pd.read_sql_query(
                trades_query, 
                connection
            )

        except Exception as e:
            print(f'There was a database error: {e}')
            trades_df_db = pd.DataFrame()
        
        finally:
            if connection:
                connection.close()
                print('Closing database connection')
            
        self.trades_df = trades_df_db

    def get_dividends_db(self):

            connection = None 
            div_df_db = pd.DataFrame()

            try:
                print('Connecting to database')
                connection = psycopg2.connect(
                    host = DB_HOST, 
                    database = DB_NAME, 
                    user = DB_USER, 
                    password = DB_PASSWORD, 
                    port = DB_PORT, 
                )

                div_query = 'SELECT * FROM dividends;'
                print('Executing query')

                div_df_db = pd.read_sql_query(
                    div_query, 
                    connection
                )

            except Exception as e:
                print(f'There was a database error: {e}')
                div_df_db = pd.DataFrame()
            
            finally:
                if connection:
                    connection.close()
                    print('Closing database connection')
                
            self.div_df = div_df_db

    def transform_file(self):
        file = pd.read_excel(self.file, skiprows=13)
        file = file.iloc[:,[1,2,3,5,6]]
        file.columns = ['dividend_date','clearing_date','description','total','balance']
        first_null = file['dividend_date'].isna().argmax()
        file = file.iloc[:first_null]

        file['dividend_date'] = pd.to_datetime(file['dividend_date'])
        file['clearing_date'] = pd.to_datetime(file['clearing_date'])
        file['dividend_date'] = file['dividend_date'].dt.date
        file['clearing_date'] = file['clearing_date'].dt.date

        file['type'] = file['description'].str.split().str[0]
        
        filtered = file.copy()

        filtered = filtered[filtered['type'].isin(['JUROS','DIVIDENDOS'])]
        filtered['ticker'] = filtered['description'].str.split().str[-3]
        filtered['quantity'] = filtered['description'].str.split().str[-1]

        filtered['quantity'] = filtered['quantity'].astype('int32')
        filtered['total'] = filtered['total'].astype('float64')
        filtered['dividend'] = (filtered['total'] / filtered['quantity']).round(2)
        filtered['portfolio'] = UNRESOLVED_PLACEHOLDER
        filtered['dividend_id'] = None

        filtered = filtered[['dividend_id', 'dividend_date', 'ticker','type', 'dividend', 'total','quantity', 'portfolio']]

        self.df = filtered

    def add_validated_dividend_id_for_missing_robust(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generates unique UUIDs only for rows where 'dividend_id' is missing (NaN/None),
        ensuring no collision and explicitly handling data types to prevent warnings.
        """
        df_copy = df.copy()
        
        # 1. Ensure the 'dividend_id' column exists, using 'object' dtype
        if 'dividend_id' not in df_copy.columns:
            # Create the column and immediately set its type to 'object' (string)
            df_copy['dividend_id'] = pd.Series([np.nan] * len(df_copy), dtype='object')
            print("💡 'dividend_id' column created as type 'object'.")
        else:
            # If it exists, cast it to 'object' to handle any prior float/mixed inference
            df_copy['dividend_id'] = df_copy['dividend_id'].astype('object')
            
        # 2. Identify the rows where 'dividend_id' is missing
        missing_mask = df_copy['dividend_id'].isna()
        num_missing = missing_mask.sum()
        
        if num_missing == 0:
            print("✅ No missing 'dividend_id' values found. DataFrame returned unchanged.")
            return df_copy
        
        # 3. Create a set of all currently existing, VALID IDs for collision check
        # Note: We can safely use .astype(str) here, as all IDs are non-NaN strings now.
        existing_ids: Set[str] = set(df_copy['dividend_id'].dropna())
        
        print(f"Starting ID generation for {num_missing} missing rows. {len(existing_ids)} existing IDs found.")
        
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
        df_copy.loc[missing_mask, 'dividend_id'] = new_ids

        print(f"✅ Successfully generated and assigned {num_missing} new, non-conflicting IDs.")
        return df_copy


    def resolve_portfolio_with_db(self,
        df: pd.DataFrame, 
        database_df: pd.DataFrame,
        asset_column: str = 'ticker', 
        portfolio_column: str = 'portfolio'
    ) -> pd.DataFrame:
        """
        1. Checks database_df for existing ticker-to-portfolio mappings.
        2. Updates the main DataFrame with found mappings.
        3. Prompts for manual input ONLY for assets still missing.
        """
        df_copy = self.df.copy()

        # --- STEP 1: DATABASE LOOKUP ---
        # Ensure database_df is cleaned for mapping
        mapping_dict = database_df.set_index(asset_column)[portfolio_column].to_dict()

        # Apply mapping where the portfolio is currently missing or placeholder
        mask = (df_copy[portfolio_column] == UNRESOLVED_PLACEHOLDER) | df_copy[portfolio_column].isna()
        
        # Map the assets to known portfolios
        df_copy.loc[mask, portfolio_column] = df_copy.loc[mask, asset_column].map(mapping_dict)

        # --- STEP 2: MANUAL INPUT FOR REMAINING ---
        # Re-evaluate unresolved indices after DB update
        unresolved_indices = df_copy[
            (df_copy[portfolio_column] == UNRESOLVED_PLACEHOLDER) | df_copy[portfolio_column].isna()
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
                    print(f"✅ Assigned '{new_portfolio.strip().upper()}' to '{asset_name}'.")
                else:
                    df_copy.loc[idx, portfolio_column] = UNRESOLVED_PLACEHOLDER
            except EOFError:
                df_copy.loc[idx, portfolio_column] = UNRESOLVED_PLACEHOLDER
            
        return df_copy

    def resolve_trades_portfolio(self):
        self.df = self.resolve_portfolio_with_db(self.df, self.trades_df, 'ticker', 'portfolio')


    def load_new_divs(self):
        buffer = StringIO()
        self.df.to_csv(buffer, header=False, index=False)
        buffer.seek(0)

        columns = self.df.columns.to_list()

        connection = None 

        try:
            print('Connection to database')
            connection = psycopg2.connect(
                host = DB_HOST, 
                database = DB_NAME, 
                user = DB_USER, 
                password = DB_PASSWORD,
                port = DB_PORT
            )

        except Exception as e:
            print(f'Database error: {e}')

        try:
            with connection.cursor() as cur:
                cur.copy_from(
                    file = buffer, 
                    table = 'dividends',
                    sep = ',',
                    columns = columns
                )
                connection.commit()
                print(f'Successfully loaded {len(self.df)} rows to dividends table')
        
        except Exception as e:
            connection.rollback()
            print(f'Database error: {e}')


if __name__ == "__main__":
    import sys
    try:
        file_path = input("Drag and drop dividends file here").strip(' "\'')
        dividend_loader = DividendsPipeline(file_path)
        dividend_loader.run_pipeline()
        print("Dividends loaded successfully!")

    except Exception as e:
        print(f'Program error: {e}')
        import traceback
        traceback.print_exc()

#TODO fix the dividend ID generator and test