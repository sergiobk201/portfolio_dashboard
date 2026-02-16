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

    def get_dividends_db(self):

        connection = None 
        dividends_df_db = pd.DataFrame()

        try:
            print('Connecting to database')
            connection = psycopg2.connect(
                host = DB_HOST, 
                database = DB_NAME, 
                username = DB_USER, 
                password = DB_PASSWORD, 
                port = DB_PORT, 
            )

            div_query = 'SELECT * FROM dividends;'
            print('Executing query')

            dividends_df_db = pd.read_sql_query(
                div_query, 
                connection
            )
        except Exception as e:
            print(f'There was a database error: {e}')
            dividends_df_db = pd.DataFrame()
        
        finally:
            if connection:
                connection.close()
                print('Closing database connection')
            
        self.div_df = dividends_df_db

    def transform_file(self):
        file = pd.read_excel(self.file, skiprows=13)
        file = file.iloc[:,[1,2,3,5,6]]
        file.columns = ['transaction_date','clearing_date','description','transaction','balance']
        first_null = file['transaction_date'].isna().argmax()
        file = file.iloc[:first_null]

        file['transaction_date'] = pd.to_datetime(file['transaction_date'])
        file['clearing_date'] = pd.to_datetime(file['clearing_date'])
        file['transaction_date'] = file['transaction_date'].dt.date
        file['clearing_date'] = file['clearing_date'].dt.date

        file['type'] = file['description'].str.split().str[0]
        
        filtered = file.copy()

        filtered = filtered[filtered['type'].isin(['JUROS','DIVIDENDS'])]
        filtered['ticker'] = filtered['description'].str.split().str[-3]
        filtered['quantity'] = filtered['description'].str.split().str[-1]

        filtered['quantity'] = filtered['quantity'].astype('int32')
        filtered['transaction'] = filtered['transaction'].astype('float64')
        filtered['ind_transaction'] = (filtered['transaction'] / filtered['quantity']).round(2)


    def add_validated_dividend_id_for_missing_robust(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generates unique UUIDs only for rows where 'dividend_id' is missing (NaN/None),
        ensuring no collision and explicitly handling data types to prevent warnings.
        """
        df_copy = self.div_df.copy()
        
        # 1. Ensure the 'dividend_id' column exists, using 'object' dtype
        if 'dividend_id' not in df_copy.columns:
            # Create the column and immediately set its type to 'object' (string)
            df_copy['t_id'] = pd.Series([np.nan] * len(df_copy), dtype='object')
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

    def load_new_divs(self):
        buffer = StringIO()


# from io import StringIO

# buffer = StringIO()

# df.to_csv(buffer, header=False, index=False)

# buffer.seek(0)

# columns = df.columns.to_list()

# Use database to simplify 



# %%
import yfinance as yf

def get_stock_sector(ticker_symbol):
    try:
        # For B3 stocks, add '.SA' (e.g., 'AAPL34.SA' or 'PETR4.SA')
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info
        
        sector = info.get('sector', 'Unknown')
        industry = info.get('industry', 'Unknown')
        
        return sector, industry
    except Exception as e:
        return None, str(e)

# Example for Apple BDR in Brazil
sector, industry = get_stock_sector("AAPL34.SA")
print(f"Sector: {sector} | Industry: {industry}")

# %%
list_tickers['ticker_search'] = list_tickers['ticker'] + ".SA"

# %%
list_tickers[['sector','industry']] = list_tickers['ticker_search'].apply(lambda x: pd.Series(get_stock_sector(x)))

# %%
list_tickers.to_excel('tickers.xlsx', index=False)

# %%
final_tickers = pd.read_excel('tickers.xlsx')
final_tickers

# %%
final_tickers = final_tickers.rename(columns={'asset':'company_name'})
final_tickers

# %%
from io import StringIO

buffer = StringIO()

final_tickers.to_csv(buffer, header=False, index=False)

buffer.seek(0)

columns = final_tickers.columns.to_list()

import pandas as pd
import numpy as np
import psycopg2
import os

# --- Configuration (Use your environment variables) ---
DB_HOST = os.environ.get("host")
DB_NAME = os.environ.get("dbname")
DB_USER = os.environ.get("user")
DB_PASSWORD = os.environ.get("password")
DB_PORT = 5432

connection = None
trades_df = pd.DataFrame() # Initialize an empty DataFrame

try:
    # 1. Establish the connection
    print(f"Connecting to the '{DB_NAME}' database...")
    connection = psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT
    )
    
except Exception as e:
    print(f"❌ Database connection or query error: {e}")
    trades_df = pd.DataFrame() # Ensure DF is empty on failure


try:
    with connection.cursor() as cur:
        cur.copy_from(
            file=buffer,
            table = 'assets',
            sep=",",
            columns=columns
        )

        connection.commit()
        print(f'Successfully inserted {len(final_tickers)} rows into trades table')

except Exception as e:
    connection.rollback()
    print(f'Database error: {e}')



#TODO: add portfolio method and divId method 