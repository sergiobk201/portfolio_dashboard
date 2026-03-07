# Imports
import yfinance as yf
import os
from brapi import Brapi
import pandas as pd
from dotenv import load_dotenv
import psycopg2
import requests
import json
import time
from datetime import date
from io import StringIO

load_dotenv()
brapi_key = os.environ.get("BRAPI_KEY")


# Importing tickers that need to be extracted

db_host = os.environ.get("host")
db_name = os.environ.get("dbname")
db_user = os.environ.get("user")
db_password = os.environ.get("password")
db_port = os.environ.get("port")


connection = None
active_tickers = pd.DataFrame()

try:
    print("Connecting to database")
    connection = psycopg2.connect(
        host=db_host,
        database=db_name,
        user=db_user,
        password=db_password,
        port=db_port,
    )

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

    active_tickers = pd.read_sql_query(query, connection)
    print("Active tickers downloaded")

except Exception as e:
    print(f"Database error: {e}")

finally:
    if connection:
        connection.close()
        print("connection closed")

# Iterative price pull for each values that are present on active_tickers
ticker_list = active_tickers["ticker"].tolist()
print(ticker_list)

# How to get a price with this token
client = Brapi(api_key=brapi_key)


# Telegram function
def send_manual_alert(ticker: str, price_date: str) -> None:
    """Sends a formatted SQL query to fix price pipeline errors"""

    token = os.environ.get("PORT_BOT_TOKEN")
    chat_id = os.environ.get("PORT_BOT_CHAT")

    sql_fix = f"UPDATE prices SET price = [ACTUAL_PRICE] WHERE ticker = '{ticker}' AND date = '{price_date}'"

    message = (
        f"<b>⚠️ Price Fetch Failed</b>\n"
        f"Ticker: <code>{ticker}</code>\n"
        f"Date: {price_date}\n\n"
        f"Run this SQL manually:\n"
        f"<pre>{sql_fix}</pre>"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram alert failed: {e}")


def get_brapi_prices(ticker_list) -> pd.DataFrame:
    token = brapi_key
    results = []
    current_date = date.today()

    for ticker in ticker_list:
        success = False
        print(f"Processing ticker {ticker}")

        # Layer 1: BRAPI
        try:
            url = f"https://brapi.dev/api/quote/{ticker}?range=1d&interval=1d&fundamental=false"
            params = {"token": token}
            response = requests.get(url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                stock = data["results"][0]
                history = stock.get("historicalDataPrice", [])

                if history:
                    latest = history[-1]

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
            send_manual_alert(ticker, str(current_date))
            results.append({"ticker": ticker, "price": 0.01, "date": current_date})

        time.sleep(2)

    return pd.DataFrame(results)


example_df = get_brapi_prices(ticker_list)
example_df["created_at"] = date.today()


def load_new_prices(df):
    buffer = StringIO()
    df.to_csv(buffer, header=False, index=False, sep="|")
    buffer.seek(0)

    columns = df.columns.to_list()

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
            cur.copy_from(file=buffer, table="prices", sep="|", columns=columns)
            connection.commit()
            print(f"successfully loaded {len(df)} rows to prices table")

    except Exception as e:
        if connection:
            connection.rollback()
        print(f"database error: {e}")

    finally:
        if connection:
            connection.close()


load_new_prices(example_df)
