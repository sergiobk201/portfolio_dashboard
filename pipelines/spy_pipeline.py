# Imports
import yfinance as yf
import os
import pandas as pd
from dotenv import load_dotenv
import psycopg2
import requests
import time
from datetime import date, timedelta
from io import StringIO


class PricePipeline:
    def __init__(self):
        load_dotenv()
        self.fmp_api_key = os.environ.get("FMP_API_KEY")
        self.db_config = {
            "host": os.environ.get("host"),
            "dbname": os.environ.get("dbname"),
            "user": os.environ.get("user"),
            "password": os.environ.get("password"),
            "port": os.environ.get("port"),
        }
        self.current_date = date.today()
        self.yesterday = self.current_date - timedelta(days=1)

    def get_db_connection(self):
        return psycopg2.connect(**self.db_config, sslmode="require", connect_timeout=10)

    def send_manual_alert(self) -> None:
        """Sends a formatted SQL query to fix price pipeline errors"""

        token = os.environ.get("PORT_BOT_TOKEN")
        chat_id = os.environ.get("PORT_BOT_CHAT")

        sql_fix = f"UPDATE benchmark SET price = [ACTUAL_PRICE] WHERE date = '{self.yesterday}'"

        message = (
            f"<b>⚠️ Price Fetch Failed</b>\n"
            f"Ticker: <code>{'SPY'}</code>\n"
            f"Date: {self.yesterday}\n\n"
            f"Run this SQL manually:\n"
            f"<pre>{sql_fix}</pre>"
        )

        url = f"https://api.telegram.org/bot{token}/sendMessage"

        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

        try:
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"Telegram alert failed: {e}")

    def get_prices(self) -> pd.DataFrame:
        print("Processing SPY fetch")
        success = False
        results = []
        # Layer 1: FMP API
        try:
            url = f"https://financialmodelingprep.com/stable/quote?symbol=SPY&apikey={self.fmp_api_key}"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if data:
                    results.append(
                        {"date": self.yesterday, "price": data[0]["previousClose"]}
                    )

                    print("Success pull from FMP API for SPY")
                    success = True

        except Exception as e:
            print(f"FMP API error: {e}")
            success = False

        # Layer 2: Yahoo Finance

        if not success:
            try:
                print("Falling back to layer 2 for SPY ")
                yf_ticker = "SPY"
                data_yf = yf.download(yf_ticker, period="5d", progress=False)

                if (
                    data_yf is not None
                    and not data_yf.empty
                    and "Close" in data_yf.columns
                ):
                    data_yf_filtered = data_yf[data_yf.index.date != self.current_date]

                    if not data_yf_filtered.empty:
                        last_price = float(data_yf_filtered["Close"].iloc[-1])
                        last_date = pd.to_datetime(data_yf_filtered.index[-1]).date()

                        if not pd.isna(last_price):
                            results.append(
                                {
                                    "date": last_date,
                                    "price": last_price,
                                }
                            )
                            success = True

                            print("Yahoo Finance pull successful for SPY")

                else:
                    print("Yahoo Finance returned no date for SPY")
                    success = False

            except Exception as e:
                print(f"Yahoo Finance error: {e} for SPY")
                success = False

        # Layer 3: Send manual request via Telegram

        if not success:
            print("All APIs failed for SPY. Sending alert")
            self.send_manual_alert()
            results.append({"date": self.yesterday, "price": 0.01})

        time.sleep(2)

        return pd.DataFrame(results)

    def load_to_db(self, df: pd.DataFrame):
        if df.empty:
            return
        df["created_at"] = self.current_date

        buffer = StringIO()
        df.to_csv(buffer, header=False, index=False, sep="|")
        buffer.seek(0)

        columns = df.columns.to_list()

        try:
            print("connection to database")
            with self.get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.copy_from(
                        file=buffer, table="benchmark", sep="|", columns=columns
                    )
                    conn.commit()
                    print(f"successfully loaded {len(df)} rows to benchmark table")

        except Exception as e:
            print(f"database error: {e}")
            raise e

    def run(self):
        df = self.get_prices()
        self.load_to_db(df)


if __name__ == "__main__":
    pipeline = PricePipeline()
    pipeline.run()
