from json import load
from typing import final
import pandas as pd
import streamlit as st
import psycopg2
import plotly.express as px
import os
from dotenv import load_dotenv
import numpy as np
from datetime import datetime

load_dotenv()
db_host = os.environ.get("host")
db_name = os.environ.get("dbname")
db_user = os.environ.get("user")
db_password = os.environ.get("password")
db_port = os.environ.get("port")


def get_connection():
    try:
        connection = psycopg2.connect(
            host=db_host,
            database=db_name,
            user=db_user,
            password=db_password,
            port=db_port,
        )

        return connection
    except Exception as e:
        print(f"Database connection error:{e}")


# UI setup

st.set_page_config(page_title="Portfolio Dashboard", page_icon="📈", layout="wide")

st.title("Portfolio Performance & Reporting")


# Data loading
@st.cache_data(ttl=3600)
def load_query(query):
    conn = get_connection()
    try:
        df = pd.read_sql_query(query, conn)
        return df
    except Exception as e:
        print(f"Query error or table does not exist: {e}")
    finally:
        conn.close()


portfolio_metrics = load_query(
    "SELECT portfolio_value, daily_return FROM portfolio_performance ORDER BY date DESC LIMIT 1"
)

year_start = datetime(datetime.now().year, 1, 1).strftime("%Y-%m-%d")

ytd_data = load_query(
    f"SELECT daily_return, date FROM portfolio_performance WHERE date>='{year_start}' ORDER BY date ASC"
)

if ytd_data is not None and not ytd_data.empty:
    ytd_data["daily_return"] = ytd_data["daily_return"].astype(float)
    log_returns = np.log(1 + ytd_data["daily_return"])
    ytd_twr = (np.exp(log_returns.sum()) - 1) * 100
else:
    ytd_twr = 0.0


if portfolio_metrics is not None and not portfolio_metrics.empty:
    current_val = round(portfolio_metrics["portfolio_value"].iloc[0], 2)
    daily_perf = round(portfolio_metrics["daily_return"].iloc[0] * 100, 2)

with st.container():
    st.markdown("### Total Portfolio Value")

    # Large Value Display
    st.markdown(
        f"<h1 style='text-align: left; color:white;'>${current_val:,.0f}</h1>",
        unsafe_allow_html=True,
    )

    sub_col1, sub_col2 = st.columns([1, 2])
    with sub_col1:
        color = "green" if daily_perf >= 0 else "red"
        st.markdown(
            f"<span style='color:{color}; font-weight:bold;'>{'▲' if daily_perf >= 0 else '▼'}{daily_perf:+.1f}% TODAY</span>",
            unsafe_allow_html=True,
        )
    with sub_col2:
        color_ytd = "green" if ytd_twr >= 0 else "red"
        st.markdown(
            f"<span style='color:{color_ytd}; font-weight:bold;'>{'▲' if ytd_twr >= 0 else '▼'} {ytd_twr:+.0f}% YTD</span>",
            unsafe_allow_html=True,
        )
