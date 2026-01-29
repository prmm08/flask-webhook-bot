# db.py
import os
import json
import time
import psycopg
from psycopg.rows import dict_row
from config import DATABASE_URL

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

def get_conn():
    # psycopg.connect supports context manager
    return psycopg.connect(DATABASE_URL, autocommit=False, sslmode="require")

def _recalc_local_avg_from_fills(fills):
    total_qty = 0.0
    total_value = 0.0
    for f in fills:
        q = float(f.get("qty", 0))
        p = float(f.get("price", 0))
        total_qty += q
        total_value += q * p
    if total_qty == 0:
        return None
    return total_value / total_qty

def create_position(symbol, side, entry_price, qty, tp_percent, sl_percent):
    fills = [{"qty": qty, "price": entry_price}]
    local_avg = entry_price
    last_dca_ts = time.time()
    sql = """
        INSERT INTO positions (
            symbol, side, entry_static, fills, local_avg,
            executed, tp_percent, sl_percent, auto_close_enabled,
            last_dca_ts, status, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, 'active', NOW(), NOW())
        RETURNING id;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                symbol, side, entry_price, json.dumps(fills),
                local_avg, 0, float(tp_percent), float(sl_percent), False, last_dca_ts
            ))
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id

def load_active_positions():
    sql = "SELECT * FROM positions WHERE status = 'active';"
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            for r in rows:
                if isinstance(r.get("fills"), str):
                    r["fills"] = json.loads(r["fills"])
            return rows

def load_position(pos_id):
    sql = "SELECT * FROM positions WHERE id = %s;"
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (pos_id,))
            r = cur.fetchone()
            if r and isinstance(r.get("fills"), str):
                r["fills"] = json.loads(r["fills"])
            return r

def add_fill(pos_id, qty, price):
    pos = load_position(pos_id)
    if not pos:
        raise ValueError("Position not found")
    fills = pos.get("fills") or []
    if isinstance(fills, str):
        fills = json.loads(fills)
    fills.append({"qty": qty, "price": price})
    local_avg = _recalc_local_avg_from_fills(fills) or pos.get("local_avg") or 0.0
    sql = """
        UPDATE positions
        SET fills = %s::jsonb,
            local_avg = %s,
            updated_at = NOW()
        WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (json.dumps(fills), local_avg, pos_id))
        conn.commit()

def update_executed(pos_id, executed):
    sql = "UPDATE positions SET executed = %s, updated_at = NOW() WHERE id = %s;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (executed, pos_id))
        conn.commit()

def enable_auto_close(pos_id):
    sql = "UPDATE positions SET auto_close_enabled = TRUE, updated_at = NOW() WHERE id = %s;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pos_id,))
        conn.commit()

def update_last_dca_ts(pos_id):
    sql = "UPDATE positions SET last_dca_ts = %s, updated_at = NOW() WHERE id = %s;"
    ts = time.time()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (ts, pos_id))
        conn.commit()

def close_position(pos_id):
    sql = "UPDATE positions SET status = 'closed', updated_at = NOW() WHERE id = %s;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pos_id,))
        conn.commit()

def list_all_positions(limit=100):
    sql = "SELECT * FROM positions ORDER BY created_at DESC LIMIT %s;"
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
            for r in rows:
                if isinstance(r.get("fills"), str):
                    r["fills"] = json.loads(r["fills"])
            return rows
