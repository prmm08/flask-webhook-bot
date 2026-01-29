# db.py
import os
import json
from typing import Optional, Dict, Any
import psycopg
from psycopg.rows import dict_row
from config import DATABASE_URL

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)

def init_schema():
    sql = """
    CREATE TABLE IF NOT EXISTS positions (
      id SERIAL PRIMARY KEY,
      symbol TEXT NOT NULL,
      side TEXT NOT NULL,
      entry_price NUMERIC NOT NULL,
      qty NUMERIC NOT NULL,
      order_id TEXT,
      fills JSONB,
      local_avg NUMERIC,
      status TEXT DEFAULT 'open',
      tp_order_id TEXT,
      sl_order_id TEXT,
      last_dca_ts TIMESTAMP,
      dca_count INTEGER DEFAULT 0,
      dca_deviation_percent NUMERIC DEFAULT 0,
      dca_volume_multiplier NUMERIC DEFAULT 1,
      tp_percent NUMERIC,
      sl_percent NUMERIC,
      created_at TIMESTAMP DEFAULT now()
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)

def create_position(symbol: str, side: str, entry_price: float, qty: float, order_id: Optional[str],
                    fills: Optional[Dict]=None, local_avg: Optional[float]=None,
                    tp_percent: Optional[float]=None, sl_percent: Optional[float]=None,
                    dca_count: int=0, dca_deviation_percent: float=0.0, dca_volume_multiplier: float=1.0) -> int:
    sql = """
    INSERT INTO positions (symbol, side, entry_price, qty, order_id, fills, local_avg, tp_percent, sl_percent, dca_count, dca_deviation_percent, dca_volume_multiplier)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    RETURNING id;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                symbol, side, entry_price, qty, order_id, json.dumps(fills or {}), local_avg,
                tp_percent, sl_percent, dca_count, dca_deviation_percent, dca_volume_multiplier
            ))
            row = cur.fetchone()
            return row["id"]

def update_position_orders(pos_id: int, tp_order_id: Optional[str], sl_order_id: Optional[str]):
    sql = "UPDATE positions SET tp_order_id=%s, sl_order_id=%s WHERE id=%s"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (tp_order_id, sl_order_id, pos_id))

def get_open_positions():
    sql = "SELECT * FROM positions WHERE status='open'"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

def append_dca(pos_id: int, added_qty: float, new_avg: float):
    sql = "UPDATE positions SET qty = qty + %s, local_avg = %s, dca_count = dca_count + 1, last_dca_ts = now() WHERE id = %s"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (added_qty, new_avg, pos_id))

def set_position_status(pos_id: int, status: str):
    sql = "UPDATE positions SET status=%s WHERE id=%s"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (status, pos_id))
