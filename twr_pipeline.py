# Imports
import os
from dotenv import load_dotenv
import pandas as pd
import psycopg2


load_dotenv()
db_config = {
    "host": os.environ.get("host"),
    "dbname": os.environ.get("dbname"),
    "user": os.environ.get("user"),
    "password": os.environ.get("password"),
    "port": os.environ.get("port"),
}

connection = psycopg2.connect(**db_config, sslmode="require", connect_timeout=10)

last_date = "SELECT date FROM prices ORDER BY date DESC LIMIT 1"
df = pd.read_sql_query(last_date, connection)
extraction_date = df.iloc[0, 0]
print(extraction_date)


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
        WHERE date <= %(date_param)s 
        GROUP BY ticker
    )
    SELECT  SUM(nt.net_positions * p.price)
    FROM net_trades nt
    INNER JOIN prices p ON p.ticker = nt.ticker
    WHERE nt.net_positions > 0
        AND p.date = %(date_param)s;"""

equity_value = pd.read_sql_query(
    query, connection, params={"date_param": extraction_date}
)
equity_value


last_twr = "SELECT * FROM portfolio_performance ORDER BY date DESC LIMIT 1"
last_perf = pd.read_sql_query(last_twr, connection)
last_perf

prev_portfolio = last_perf["porfolio_value"]
prev_equity = last_perf["equity_value"]
prev_uninvested_cash = last_perf["uninvested_cash"]
prev_total_invested = last_perf["total_invested"]
prev_external_inflow = last_perf["external_inflow"]
prev_twr = last_perf["twr"]
