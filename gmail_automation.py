### Imports 
import os
import datetime
import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import shutil
import pikepdf
from pathlib import Path
import pdfplumber
import pandas as pd
import numpy as np
from typing import Union
import re
import psycopg2
import uuid
from typing import Set
from io import StringIO
from dotenv import load_dotenv

load_dotenv()
pdf_password = os.getenv('PASSWORD_PDF')
UNRESOLVED_PLACEHOLDER = '!!!_INPUT_REQUIRED_!!!'

# --- Supabase env variables
DB_HOST = os.environ.get("host")
DB_NAME = os.environ.get("dbname")
DB_USER = os.environ.get("user")
DB_PASSWORD = os.environ.get("password")
DB_PORT = os.environ.get("port")


class GmailRicoImporter:

    def __init__(self):
        pass

    def gmail_import(self):

        # If you modify these scopes, delete the file token.json
        SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
        creds = None
 
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        if os.path.exists("token.json"):
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
                )
                creds = flow.run_local_server(port=0)
                # Save the credentials for the next run
            with open("token.json", "w") as token:
                token.write(creds.to_json())

        try:
            # Build gmail service
            service = build("gmail", "v1", credentials=creds)

            # Obtain the last email (30 day window)
            response = service.users().messages().list(
                userId="me",
                maxResults=1,
                q="from: noreply@rico.com.vc has:attachment newer_than:30d"  # filter is noreply with attachment (the email may change, verify and add more domains if needed)
            ).execute()

            messages = response.get("messages", [])

            if not messages:
                print("No trade statements")
            else:
                print("Last trade statement obtained")

            last_msg_id = messages[0]["id"]

            # 2. Get the entire email content
            msg = service.users().messages().get(
                userId="me",
                id=last_msg_id,
                format="full"
            ).execute()

            # 3. Search for attachment
            def walk_parts(part):
                parts = []
                if part.get("parts"):
                    for p in part["parts"]:
                        parts.extend(walk_parts(p))
                else:
                    parts.append(part)
                return parts

            all_parts = walk_parts(msg["payload"])

            # 4. Download the attachment
            for part in all_parts:
                filename = part.get("filename")
                body = part.get("body", {})

                if filename and body.get("attachmentId"):
                    attachment_id = body["attachmentId"]

                    # GET attachment request
                    att = service.users().messages().attachments().get(
                        userId="me",
                        messageId=last_msg_id,
                        id=attachment_id
                    ).execute()

                    import base64, os
                    file_data = base64.urlsafe_b64decode(att["data"])
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    filename = f'trade_confirmation_{today}.pdf'

                    # Guardar el archivo localmente
                    os.makedirs("attachments", exist_ok=True)
                    path = os.path.join("attachments", filename)

                    with open(path, "wb") as f:
                        f.write(file_data)

                    print(f"Attachment descargado: {path}")

        except Exception as e:
            print("Error:", e)



        today = datetime.datetime.now().strftime("%Y-%m-%d")
        self.today = today

        attachments_dir = os.path.join(os.getcwd(), "attachments/trade_confirmation")
        pdf_path = f'{attachments_dir}_{today}.pdf'

        self.pdf_path = pdf_path

    def decrypt_pdf(self, input_path, output_path, password):
        """Open password-protected PDF and save unencrypted copy."""
        with pikepdf.open(input_path, password=password) as pdf:
            pdf.save(output_path)

    def final_pdf(self):

        input_pdf = self.pdf_path
        output_pdf = f'statement_unlocked_{self.today}.pdf'
        self.output_pdf = output_pdf


        self.decrypt_pdf(input_pdf, output_pdf, pdf_password)
        print("Decrypted PDF saved:", output_pdf)

        try: 
            os.remove(self.pdf_path)
            print(f"Removed encrypted PDF: {self.pdf_path}")
        except Exception as e:
            print(f"Error removing file {self.pdf_path}: {e}")


        src = os.path.join(os.getcwd(), output_pdf)
        dst = os.path.join(os.getcwd(), "attachments")

        self.dst = dst

        shutil.move(src, dst)
        print(f"Moved decrypted PDF to: {dst}")

    def read_pdf(self):
      
        with pdfplumber.open(os.path.join(self.dst, self.output_pdf)) as pdf:
            # Slicing from index 0 to 5 (pages 1 to 5)
            target_pages = pdf.pages[0:5] 
            
            full_text = ""
            for page in target_pages:
                page_text = page.extract_text()
                if page_text: # Handle cases where a page might be an image/blank
                    full_text += page_text + "\n"
                    
            self.full_text = full_text

        trades = re.findall(r"^1-BOVESPA.*$", full_text, flags=re.MULTILINE)
        
        # 2. Parse each line into structured data
        records = []
        for line in trades:
            parts = line.split()

            market = parts[0]                      # 1-BOVESPA
            side = parts[1]                        # C or V
            tipo = parts[2]                        # FRACIONARIO, VISTA, etc.

            # Extract description until '@'
            result = next((s for s in parts if s.isdigit()), None)
            index = parts.index(result)
            description = " ".join(parts[3:index -1])

            quantity = int(parts[index])

            # Convert Brazilian decimals 34,17 → 34.17
            price = round(float(parts[index + 1].replace(",", ".")),2)
            total = round(float(parts[index + 2].replace(".", "").replace(",", ".")),2)

            records.append({
                "market": market,
                "side": side,
                "type": tipo,
                "asset": description,
                "quantity": quantity,
                "price": price,
                "total": total,
            })

        # 3. Convert to DataFrame
        df = pd.DataFrame(records)
        self.df = df


    def extract_single_value(self, label, text):
        pattern = fr"{label}\s+([\d\.,]+)"
        match = re.search(pattern, text)
        return match.group(1) if match else None
    
    def extract_fees(self):

        total_cblc = self.extract_single_value("Total CBLC", self.full_text)
        print("Total CBLC:", total_cblc)

        total_bovespa = self.extract_single_value('Total Bovespa / Soma', self.full_text)
        print('Total Bovespa', total_bovespa)

        total_custos = self.extract_single_value('Total Custos / Despesas', self.full_text)
        print('Total Custos', total_custos)

        total_cblc = round(float(total_cblc.replace(".", "").replace(",", ".")),2)
        total_bovespa = round(float(total_bovespa.replace(".", "").replace(",", ".")),2)
        total_custos = round(float(total_custos.replace(".", "").replace(",", ".")),2)

        self.total_cblc = total_cblc
        self.total_bovespa = total_bovespa
        self.total_custos = total_custos


    def extract_first_date(self, text):
        pattern = r"\b(\d{2}/\d{2}/\d{4})\b"
        match = re.search(pattern, text)
        return match.group(1) if match else None
    
    def add_data_to_df(self):

        trade_date = self.extract_first_date(self.full_text)
        self.trade_date = trade_date

        total_trade_volume = self.df['total'].sum()
        total_fees = (self.total_cblc - total_trade_volume + self.total_custos + self.total_bovespa).round(2)

        self.df['fee'] = round(self.df['total'] / total_trade_volume * total_fees, 2)
        self.df['ticker'] = None

    def get_trades_table(self):

        TARGET_TABLE = "trades" 

        connection = None
        db_trades_df = pd.DataFrame() # Initialize an empty DataFrame

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

            self.connection = connection
            
            # 2. Define the SQL query
            sql_query = f"SELECT asset, ticker, portfolio FROM {TARGET_TABLE};"
            
            # 3. Use pandas to execute the query and read directly into a DataFrame
            print(f"Executing query: {sql_query}")
            db_trades_df = pd.read_sql_query(
                sql_query, 
                connection # pandas handles the cursor and fetching internally
            )
            
            # 4. Success message and data inspection
            print("\n--- ✅ Data Extraction Successful ---")
            print(f"DataFrame shape: {db_trades_df.shape}")
            print("\n--- DataFrame Head ---")
            print(db_trades_df.head())
            
        except Exception as e:
            print(f"❌ Database connection or query error: {e}")
            db_trades_df = pd.DataFrame() # Ensure DF is empty on failure

        finally:
            # Always close the connection
            if connection:
                connection.close()
                print("\nDatabase connection closed.")

        self.db_trades_df = db_trades_df

    def resolve_tickers_with_db(
        df: pd.DataFrame, 
        database_df: pd.DataFrame,
        asset_column: str = 'asset', 
        ticker_column: str = 'ticker'
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
            (df_copy[ticker_column] == UNRESOLVED_PLACEHOLDER) | df_copy[ticker_column].isna()
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
                df_copy.loc[df_copy[asset_column] == asset_name, ticker_column] = resolved_ticker
                print(f"🗄️ DB-resolved '{asset_name}' -> '{resolved_ticker}'.")
                continue

            # --- PRIORITY 2: INTERNAL LOOKUP (Check current DF for existing values) ---
            internal_check = df_copy[
                (df_copy[asset_column] == asset_name) & 
                (df_copy[ticker_column] != UNRESOLVED_PLACEHOLDER) & 
                df_copy[ticker_column].notna()
            ][ticker_column].unique()

            if len(internal_check) >= 1:
                resolved_ticker = internal_check[0]
                df_copy.loc[df_copy[asset_column] == asset_name, ticker_column] = resolved_ticker
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
                        df_copy.loc[df_copy[asset_column] == asset, ticker_column] = ticker_val
                        print(f"✅ Ticker '{ticker_val}' assigned to '{asset}'.")
                    else:
                        df_copy.loc[df_copy[asset_column] == asset, ticker_column] = UNRESOLVED_PLACEHOLDER
                except EOFError:
                    df_copy.loc[df_copy[asset_column] == asset, ticker_column] = UNRESOLVED_PLACEHOLDER
            
        print("----------------------------------\n")
        return df_copy

    def resolve_trades_df(self):
        self.df = self.resolve_tickers_with_db(self.df, self.db_trades_df, 'asset','ticker')

        format_date = "%d/%m/%Y"

        date_object = datetime.strptime(self.trade_date, format_date)
        self.df['date'] = date_object.date()

        current_cols = self.df.columns.tolist()

        new_order = ['date', 'side', 'ticker', 'asset', 'quantity', 'price','total','fee']

        # 1. Define the mapping
        mapping = {
            'C': 'BUY',
            'V': 'SELL'
        }

        # 2. Apply the mapping to the specific column
        #    (The 'inplace=True' changes the DataFrame directly)
        self.df['side'] = self.df['side'].replace(mapping)

        self.df = self.df[new_order]
        self.df['portfolio'] = None



    def resolve_portfolio_with_db(
        df: pd.DataFrame, 
        database_df: pd.DataFrame,
        asset_column: str = 'asset', 
        portfolio_column: str = 'portfolio'
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
        self.df = self.resolve_portfolio_with_db(self.db, self.db_trades_df, 'ticker', 'portfolio')


    def add_validated_trade_id_for_missing_robust(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generates unique UUIDs only for rows where 'trade_id' is missing (NaN/None),
        ensuring no collision and explicitly handling data types to prevent warnings.
        """
        df_copy = df.copy()
        trades_df_copy = self.db_trades_df

        # 1. Ensure the 'trade_id' column exists, using 'object' dtype
        if 'trade_id' not in df_copy.columns:
            # Create the column and immediately set its type to 'object' (string)
            df_copy['trade_id'] = pd.Series([np.nan] * len(df_copy), dtype='object')
            print("💡 'trade_id' column created as type 'object'.")
        else:
            # If it exists, cast it to 'object' to handle any prior float/mixed inference
            trades_df_copy['trade_id'] = trades_df_copy['trade_id'].astype('object')
            
        # 2. Identify the rows where 'trade_id' is missing
        missing_mask = df_copy['trade_id'].isna()
        num_missing = missing_mask.sum()
        
        if num_missing == 0:
            print("✅ No missing 'trade_id' values found. DataFrame returned unchanged.")
            return df_copy
        
        # 3. Create a set of all currently existing, VALID IDs for collision check
        # Note: We can safely use .astype(str) here, as all IDs are non-NaN strings now.
        existing_ids: Set[str] = set(trades_df_copy['trade_id'].dropna())
        
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
        df_copy.loc[missing_mask, 'trade_id'] = new_ids

        print(f"✅ Successfully generated and assigned {num_missing} new, non-conflicting IDs.")
        return df_copy

    def validate_trade_ids(self):
        self.df = self.add_validated_trade_id_for_missing_robust(self.df)

    def final_transformation(self):
        final_order = ['trade_id','date', 'side', 'ticker', 'asset', 'quantity', 'price','total','fee','portfolio']
        self.df = self.df[final_order]


    def load_to_db(self):
        buffer = StringIO()
        self.df.to_csv(buffer, header=False, index=False)

        buffer.seek(0)

        columns = self.df.columns.to_list()
        try:
            # 1. Establish the connection
            connection = psycopg2.connect(
                host=DB_HOST,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                port=DB_PORT
            )
            
            with connection.cursor() as cur:
                cur.copy_from(
                    file=buffer,
                    table = 'trades',
                    sep=",",
                    columns=columns
                )

                connection.commit()
                print(f'Successfully inserted {len(self.df)} rows into trades table')

        except Exception as e:
            self.connection.rollback()
            print(f'Database error: {e}')

        finally:
            connection.cursor.close()
            connection.close()

if __name__ == "__main__":
    import sys
    try:
        GmailRicoImporter()
        print("Trade statement import successful!")
    except Exception as e:
        print(f'Program error: {e}')




