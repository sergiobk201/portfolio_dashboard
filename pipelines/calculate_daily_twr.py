"""
calculate_daily_twr.py
──────────────────────
Runs every day via GitHub Actions to:
  1. Append yesterday's TWR row to price_performance
  2. Recalculate + upsert the last 35 days to absorb any late-arriving
     trades (T+2 settlement) or dividends (T+30)

The anchor point is the last settled row older than 35 days — everything
after that is recalculated from scratch on each run, so corrections flow
through automatically without any manual intervention.

Required env vars (GitHub Secrets):
  host                 – DB Host
  dbname               – DB Name
  user                 – DB User
  password             – DB Password
  port                 – DB Port
  TELEGRAM_BOT_TOKEN   – from @BotFather
  TELEGRAM_CHAT_ID     – your personal or group chat ID

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ONE-TIME DB SETUP
Run both functions once in the SQL editor before first use.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 1. Equity value for a single date (used by backfill / manual checks)
CREATE OR REPLACE FUNCTION get_equity_value(as_of_date date)
RETURNS numeric LANGUAGE sql AS $$
  SELECT COALESCE(SUM(h.net_qty * p.price), 0)
  FROM (
    SELECT ticker,
           SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END) AS net_qty
    FROM trades
    WHERE date <= as_of_date
    GROUP BY ticker
    HAVING SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END) > 0
  ) h
  JOIN prices p ON p.ticker = h.ticker AND p.date = as_of_date
$$;

-- 2. Equity values for a date range (used by the rolling window)
--    Returns one row per trading day that has prices.
--    Days with no prices (weekends / holidays) are absent — Python ffills them.
CREATE OR REPLACE FUNCTION get_equity_values_range(p_start date, p_end date)
RETURNS TABLE(date date, equity_value numeric) LANGUAGE sql AS $$
  WITH price_dates AS (
    SELECT DISTINCT date FROM prices
    WHERE date BETWEEN p_start AND p_end
  ),
  holdings_per_day AS (
    SELECT
      pd.date,
      t.ticker,
      SUM(CASE WHEN t.side = 'BUY' THEN t.quantity ELSE -t.quantity END) AS net_qty
    FROM price_dates pd
    JOIN trades t ON t.date <= pd.date
    GROUP BY pd.date, t.ticker
    HAVING SUM(CASE WHEN t.side = 'BUY' THEN t.quantity ELSE -t.quantity END) > 0
  )
  SELECT
    h.date,
    COALESCE(SUM(h.net_qty * p.price), 0) AS equity_value
  FROM holdings_per_day h
  JOIN prices p ON p.ticker = h.ticker AND p.date = h.date
  GROUP BY h.date
  ORDER BY h.date
$$;

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import sys
from datetime import date, timedelta

import pandas as pd
import requests
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host": os.environ.get("host"),
    "dbname": os.environ.get("dbname"),
    "user": os.environ.get("user"),
    "password": os.environ.get("password"),
    "port": os.environ.get("port"),
}
TG_TOKEN = os.environ["PORT_BOT_TOKEN"]
TG_CHAT_ID = os.environ["PORT_BOT_CHAT"]
TABLE = "portfolio_performance"
WINDOW_DAYS = 35  # how far back to recalculate (covers T+2 trades + T+30 divs)

# ── Helpers ───────────────────────────────────────────────────────────────────


def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    return psycopg2.connect(**DB_CONFIG, sslmode="require")


def send_telegram(message: str) -> None:
    """Fire-and-forget Telegram alert."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data, timeout=10)
        r.raise_for_status()
        print(f"[TWR] 📨 Telegram sent: {message[:80].strip()}...")
    except Exception as e:
        print(f"[TWR] WARNING: Telegram alert failed — {e}")


def pct(v: float) -> str:
    return f"{v * 100:.2f}%"


# ── Connect, Fetch, Calculate & Upsert ────────────────────────────────────────
with get_db_connection() as conn:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # ── Date window ────────────────────────────────────────────────────────
        yesterday = date.today() - timedelta(days=1)
        window_start = yesterday - timedelta(days=WINDOW_DAYS - 1)
        window_end = yesterday

        print(f"[TWR] Window: {window_start} → {window_end}  ({WINDOW_DAYS} days)")

        # ── 1. Anchor row ──────────────────────────────────────────────────────
        cur.execute(
            f"""
            SELECT date, portfolio_value, uninvested_cash, total_invested, twr, equity_value 
            FROM {TABLE}
            WHERE date < %s
            ORDER BY date DESC
            LIMIT 1
        """,
            (window_start.isoformat(),),
        )
        anchor = cur.fetchone()

        if not anchor:
            print("[TWR] ERROR: no settled anchor row found before the rolling window.")
            print("             Run a full backfill first, then re-run this script.")
            send_telegram(
                "🚨 *TWR Job Failed*\n"
                "No anchor row found outside the 35-day window.\n"
                "_Run a full backfill first._"
            )
            sys.exit(1)

        anchor_date = anchor["date"]
        anchor_portfolio = float(anchor["portfolio_value"])
        anchor_cash = float(anchor["uninvested_cash"])
        anchor_total_inv = float(anchor["total_invested"])
        anchor_twr = float(anchor["twr"])
        anchor_equity = float(anchor["equity_value"])

        print(
            f"[TWR] Anchor     : {anchor_date}  portfolio={anchor_portfolio:,.2f}  twr={pct(anchor_twr)}"
        )

        # ── 2. Batch load equity values for the entire window ──────────────────
        cur.execute(
            "SELECT * FROM get_equity_values_range(%s, %s)",
            (window_start.isoformat(), window_end.isoformat()),
        )
        equity_data = cur.fetchall()

        if not equity_data:
            send_telegram(
                f"🚨 *TWR Job Failed* — {yesterday}\n"
                f"`get_equity_values_range()` returned nothing.\n"
                f"_Prices may be missing for this window._"
            )
            print("[TWR] ERROR: get_equity_values_range() returned no data.")
            sys.exit(1)

        equity_by_date = {
            row["date"].isoformat()
            if isinstance(row["date"], date)
            else row["date"]: float(row["equity_value"])
            for row in equity_data
        }
        print(f"[TWR] Equity rows fetched: {len(equity_by_date)} trading days")

        # ── 3. Batch load trades in the window ────────────────────────────────
        cur.execute(
            """
            SELECT date, side, total, fee 
            FROM trades 
            WHERE date >= %s AND date <= %s
        """,
            (window_start.isoformat(), window_end.isoformat()),
        )
        trades_data = cur.fetchall()

        trades_by_date: dict[str, dict] = {}
        for r in trades_data:
            d = r["date"].isoformat() if isinstance(r["date"], date) else r["date"]
            if d not in trades_by_date:
                trades_by_date[d] = {"buys": 0.0, "sells": 0.0}
            cost = float(r["total"])
            fee = float(r["fee"])
            if r["side"] == "BUY":
                trades_by_date[d]["buys"] += cost + fee
            else:
                trades_by_date[d]["sells"] += cost - fee

        print(f"[TWR] Trade dates in window: {len(trades_by_date)}")

        # ── 4. Batch load dividends in the window ──────────────────────────────
        cur.execute(
            """
            SELECT dividend_date, total 
            FROM dividends 
            WHERE dividend_date >= %s AND dividend_date <= %s
        """,
            (window_start.isoformat(), window_end.isoformat()),
        )
        divs_data = cur.fetchall()

        divs_by_date: dict[str, float] = {}
        for r in divs_data:
            d = (
                r["dividend_date"].isoformat()
                if isinstance(r["dividend_date"], date)
                else r["dividend_date"]
            )
            divs_by_date[d] = divs_by_date.get(d, 0.0) + float(r["total"])

        print(f"[TWR] Dividend dates in window: {len(divs_by_date)}")

        # ── 5. Walk forward through the window, recalculating each day ────────
        prev_portfolio = anchor_portfolio
        prev_cash = anchor_cash
        prev_total_inv = anchor_total_inv
        prev_twr = anchor_twr
        prev_equity = anchor_equity

        rows_to_upsert = []

        current = window_start
        while current <= window_end:
            date_str = current.isoformat()

            equity_value = equity_by_date.get(date_str, prev_equity)
            flows = trades_by_date.get(date_str, {"buys": 0.0, "sells": 0.0})
            buys = flows["buys"]
            sells = flows["sells"]
            divs = divs_by_date.get(date_str, 0.0)

            cash_before_inflow = prev_cash + sells + divs - buys
            external_inflow = max(0.0, -cash_before_inflow)
            uninvested_cash = cash_before_inflow + external_inflow

            portfolio_value = equity_value + uninvested_cash
            total_invested = prev_total_inv + external_inflow

            denom = prev_portfolio + external_inflow
            daily_return = float(portfolio_value / denom - 1) if denom != 0 else 0.0
            twr = float((1 + prev_twr) * (1 + daily_return) - 1)

            rows_to_upsert.append(
                {
                    "date": date_str,
                    "portfolio_value": round(portfolio_value, 6),
                    "equity_value": round(equity_value, 6),
                    "uninvested_cash": round(uninvested_cash, 6),
                    "total_invested": round(total_invested, 6),
                    "external_inflow": round(external_inflow, 6),
                    "daily_return": round(daily_return, 8),
                    "twr": round(twr, 8),
                }
            )

            prev_portfolio = portfolio_value
            prev_cash = uninvested_cash
            prev_total_inv = total_invested
            prev_twr = twr
            prev_equity = equity_value

            current += timedelta(days=1)

        print(f"[TWR] Rows calculated: {len(rows_to_upsert)}")

        # ── 6. Upsert all rows in one batch ──────────────────────────────────
        upsert_query = f"""
            INSERT INTO {TABLE} (
                date, portfolio_value, equity_value, uninvested_cash, 
                total_invested, external_inflow, daily_return, twr
            ) VALUES %s
            ON CONFLICT (date) DO UPDATE SET
                portfolio_value = EXCLUDED.portfolio_value,
                equity_value = EXCLUDED.equity_value,
                uninvested_cash = EXCLUDED.uninvested_cash,
                total_invested = EXCLUDED.total_invested,
                external_inflow = EXCLUDED.external_inflow,
                daily_return = EXCLUDED.daily_return,
                twr = EXCLUDED.twr
        """
        data_tuples = [
            (
                r["date"],
                r["portfolio_value"],
                r["equity_value"],
                r["uninvested_cash"],
                r["total_invested"],
                r["external_inflow"],
                r["daily_return"],
                r["twr"],
            )
            for r in rows_to_upsert
        ]
        execute_values(cur, upsert_query, data_tuples)
        conn.commit()
        print(
            f"[TWR] ✓ Upserted {len(rows_to_upsert)} rows  ({window_start} → {window_end})"
        )

# ── 7. Sanity checks on yesterday's row → Telegram alerts ────────────────────
#
#   Run checks on yesterday (the freshest row) only.
#   Three independent triggers:
#
#   A. Equity moved > ±10% vs the day before yesterday
#      → likely bad price data or corrupted holdings
#
#   B. Daily return > ±10%
#      → implausibly large single-day move
#
#   C. Cumulative TWR is negative
#      → portfolio is underwater on a time-weighted basis
#
yesterday_row = rows_to_upsert[-1]  # last item in the window = yesterday
prev_row = rows_to_upsert[-2] if len(rows_to_upsert) >= 2 else None

alerts = []

# A — Equity spike / drop
if prev_row and float(prev_row["equity_value"]) > 0:
    eq_change = (
        float(yesterday_row["equity_value"]) - float(prev_row["equity_value"])
    ) / float(prev_row["equity_value"])
    if abs(eq_change) > 0.10:
        alerts.append(
            f"⚠️ *Equity moved {pct(eq_change)}* — {yesterday}\n"
            f"Day before : {float(prev_row['equity_value']):,.2f}\n"
            f"Yesterday  : {float(yesterday_row['equity_value']):,.2f}\n"
            f"_Check prices table and holdings for {yesterday}_"
        )

# B — Daily return > ±10%
daily_r = float(yesterday_row["daily_return"])
if abs(daily_r) > 0.10:
    alerts.append(
        f"⚠️ *Large daily return: {pct(daily_r)}* — {yesterday}\n"
        f"Portfolio : {float(yesterday_row['portfolio_value']):,.2f}\n"
        f"_Verify prices and any large trades on this date._"
    )

# C — TWR negative
twr_val = float(yesterday_row["twr"])
if twr_val < 0:
    alerts.append(
        f"🔴 *Cumulative TWR is negative* — {yesterday}\n"
        f"TWR      : {pct(twr_val)}\n"
        f"Daily r  : {pct(daily_r)}\n"
        f"Portfolio: {float(yesterday_row['portfolio_value']):,.2f}\n"
        f"_Portfolio is underwater on a time-weighted basis._"
    )

for alert in alerts:
    send_telegram(alert)

# ── 8. Final summary ──────────────────────────────────────────────────────────
y = yesterday_row
print("=" * 48)
print("  YESTERDAY SUMMARY")
print("=" * 48)
print(f"  Date            : {y['date']}")
print(f"  Portfolio Value : {float(y['portfolio_value']):>16,.2f}")
print(f"    Equity        : {float(y['equity_value']):>16,.2f}")
print(f"    Cash          : {float(y['uninvested_cash']):>16,.2f}")
print(f"  Total Invested  : {float(y['total_invested']):>16,.2f}")
print(f"  External Inflow : {float(y['external_inflow']):>16,.2f}")
print(f"  Daily Return    : {float(y['daily_return']):>15.4%}")
print(f"  Cumulative TWR  : {float(y['twr']):>15.4%}")
print("=" * 48)
