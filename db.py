import os
import psycopg2
from psycopg2.extras import RealDictCursor

# Render Connection String
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
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        # Tabelle bleibt gleich, SL Spalte existiert noch, wird aber ignoriert
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
    except Exception as e:
        print(f"Init DB Error: {e}")
    finally:
        conn.close()

def add_pending_trade(data):
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        # WICHTIG: Wir fügen bei sl_percent hart eine 0 ein
        cur.execute("""
            INSERT INTO trades (symbol, direction, leverage, trade_size, tp_percent, sl_percent, status)
            VALUES (%s, %s, %s, %s, %s, 0, 'PENDING')
        """, (
            data['symbol'], data['direction'], data['leverage'], 
            data['trade_size'], data['tp_percent']
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

def get_open_trade_count():
    conn = get_conn()
    if not conn: return 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as count FROM trades WHERE status = 'OPEN'")
        result = cur.fetchone()
        return result['count'] if result else 0
    except: return 0
    finally:
        conn.close()

def check_trade_exists(trade_id):
    conn = get_conn()
    if not conn: return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM trades WHERE id = %s", (trade_id,))
        return cur.fetchone() is not None
    except: return False
    finally:
        conn.close()

def is_symbol_active(symbol):
    conn = get_conn()
    if not conn: return False
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM trades 
            WHERE symbol = %s AND status IN ('OPEN', 'PENDING') 
            LIMIT 1
        """, (symbol,))
        return cur.fetchone() is not None
    except Exception as e:
        return True 
    finally:
        conn.close()