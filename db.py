import os
import psycopg2
from psycopg2.extras import RealDictCursor
import logging

# Render setzt diese Variable automatisch, wenn du die DB verbindest
DB_URL = os.getenv("DATABASE_URL")

def get_conn():
    if not DB_URL:
        print("FEHLER: DATABASE_URL nicht gesetzt!")
        return None
    try:
        conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        print(f"DB Connection Error: {e}")
        return None

def init_db():
    """Erstellt die Tabelle automatisch beim Start, falls nicht vorhanden"""
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                status VARCHAR(20) DEFAULT 'PENDING',
                leverage INT,
                trade_size FLOAT,
                entry_price FLOAT DEFAULT 0,
                avg_price FLOAT DEFAULT 0,
                quantity FLOAT DEFAULT 0,
                tp_percent FLOAT,
                sl_percent FLOAT,
                dca_level INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()
        print("Datenbank-Tabelle 'trades' überprüft/erstellt.")
    except Exception as e:
        print(f"Init DB Error: {e}")
    finally:
        conn.close()

def add_pending_trade(data):
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trades (symbol, direction, leverage, trade_size, tp_percent, sl_percent, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
        """, (
            data['symbol'], data['direction'], data['leverage'], 
            data['trade_size'], data['tp_percent'], data['sl_percent']
        ))
        conn.commit()
    finally:
        conn.close()

def get_pending_trades():
    conn = get_conn()
    if not conn: return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades WHERE status = 'PENDING' ORDER BY created_at ASC")
        return cur.fetchall()
    finally:
        conn.close()

def get_open_trades():
    conn = get_conn()
    if not conn: return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades WHERE status = 'OPEN'")
        return cur.fetchall()
    finally:
        conn.close()

def update_trade_execution(trade_id, price, qty):
    """Markiert Trade als OPEN und speichert Entry"""
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE trades 
            SET status = 'OPEN', entry_price = %s, avg_price = %s, quantity = %s, updated_at = NOW()
            WHERE id = %s
        """, (price, price, qty, trade_id))
        conn.commit()
    finally:
        conn.close()

def update_dca(trade_id, level, new_avg, new_qty):
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE trades 
            SET dca_level = %s, avg_price = %s, quantity = %s, updated_at = NOW()
            WHERE id = %s
        """, (level, new_avg, new_qty, trade_id))
        conn.commit()
    finally:
        conn.close()

def close_trade(trade_id):
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("UPDATE trades SET status = 'CLOSED', updated_at = NOW() WHERE id = %s", (trade_id,))
        conn.commit()
    finally:
        conn.close()

def fail_trade(trade_id):
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("UPDATE trades SET status = 'ERROR', updated_at = NOW() WHERE id = %s", (trade_id,))
        conn.commit()
    finally:
        conn.close()