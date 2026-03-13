# Imports
import yfinance as yf
import os
import pandas as pd
from dotenv import load_dotenv
import psycopg2
import requests
import json
import time
from datetime import date
from io import StringIO


class PricePipeline:
    def __init__(self):
        load_dotenv()
        self.brapi_key = os.environ.get("BRAPI_KEY")
        self.db_config = {
            "host": os.environ.get("host"),
            "dbname": os.environ.get("dbname"),
            "user": os.environ.get("user"),
            "password": os.environ.get("password"),
            "port": os.environ.get("port"),
        }
        self.current_date = date.today()

    def get_db_connection(self):
        return psycopg2.connect(**self.db_config, sslmode="require", connect_timeout=10)

    def fetch_active_tickers(self):

        query = """
            WITH net_trades AS (
                SELECT ticker, 
                    SUM(
                        CASE 
                            WHEN side = 'BUY' THEN 1
                            WHEN side = 'SELL' THEN -1
                            ELSE 0 
                        END * quantity
                    ) AS net_positions
                FROM trades
                GROUP BY ticker
            )
            SELECT ticker
            FROM net_trades
            WHERE net_positions > 0
            ORDER BY ticker ASC; """

        try:
            with self.get_db_connection() as conn:
                active_tickers = pd.read_sql_query(query, conn)
                print("Active tickers downloaded")
                return active_tickers["ticker"].tolist()

        except Exception as e:
            print(f"Database error: {e}")
            raise e

    def send_manual_alert(self, ticker: str) -> None:
        """Sends a formatted SQL query to fix price pipeline errors"""

        token = os.environ.get("PORT_BOT_TOKEN")
        chat_id = os.environ.get("PORT_BOT_CHAT")

        sql_fix = f"UPDATE prices SET price = [ACTUAL_PRICE] WHERE ticker = '{ticker}' AND date = '{self.current_date}'"

        message = (
            f"<b>⚠️ Price Fetch Failed</b>\n"
            f"Ticker: <code>{ticker}</code>\n"
            f"Date: {self.current_date}\n\n"
            f"Run this SQL manually:\n"
            f"<pre>{sql_fix}</pre>"
        )

        url = f"https://api.telegram.org/bot{token}/sendMessage"

        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

        try:
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"Telegram alert failed: {e}")

    def get_prices(self, ticker_list) -> pd.DataFrame:
        results = []

        for ticker in ticker_list:
            success = False
            print(f"Processing ticker {ticker}")

            # Layer 1: BRAPI
            try:
                url = f"https://brapi.dev/api/quote/{ticker}?range=1d&interval=1d&fundamental=false"
                params = {"token": self.brapi_key}
                response = requests.get(url, params=params, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    stock = data["results"][0]
                    history = stock.get("historicalDataPrice", [])

                    if history:
                        latest = history[-1]

                        if latest["close"] is not None:
                            clean_date = pd.to_datetime(latest["date"], unit="s").date()

                            results.append(
                                {
                                    "ticker": stock["symbol"],
                                    "price": latest["close"],
                                    "date": clean_date,
                                }
                            )

                            print(f"Success pull from BRAPI for ticker {ticker}")
                            success = True

            except Exception as e:
                print(f"Brapi error: {e}")

            # Layer 2: Yahoo Finance

            if not success:
                try:
                    print(f"Falling back to layer 2 for ticker {ticker}")
                    yf_ticker = f"{ticker}.SA" if "-" not in ticker else ticker
                    data_yf = yf.download(yf_ticker, period="1d", progress=False)

                    if (
                        data_yf is not None
                        and not data_yf.empty
                        and "Close" in data_yf.columns
                    ):
                        last_price = data_yf["Close"].iloc[-1]

                        if not pd.isna(last_price):
                            last_date = pd.to_datetime(data_yf.index[-1]).date()
                            results.append(
                                {
                                    "ticker": ticker,
                                    "price": float(last_price),
                                    "date": last_date,
                                }
                            )
                            print(f"Yahoo Finance pull successful for ticker: {ticker}")
                            success = True

                    else:
                        print(f"Yahoo Finance returned no date for {ticker}")
                        success = False

                except Exception as e:
                    print(f"Yahoo Finance error: {e} for {ticker}")
                    success = False

            # Layer 3: Send manual request via Telegram

            if not success:
                print(f"All APIs failed for {ticker}. Sending alert")
                self.send_manual_alert(ticker)
                results.append(
                    {"ticker": ticker, "price": 0.01, "date": self.current_date}
                )

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

        connection = None

        try:
            print("connection to database")
            with self.get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.copy_from(file=buffer, table="prices", sep="|", columns=columns)
                    conn.commit()
                    print(f"successfully loaded {len(df)} rows to prices table")

        except Exception as e:
            print(f"database error: {e}")
            raise e

    def run(self):
        if date.today().weekday() >= 5:
            print("Weekend detected. Skipping fetch today")
            return

        tickers = self.fetch_active_tickers()
        if not tickers:
            print("No active tickers")
            return
        df = self.get_prices(tickers)
        self.load_to_db(df)


if __name__ == "__main__":
    pipeline = PricePipeline()
    pipeline.run()
