import psycopg2
import psycopg2.extras
import time
from config import DATABASE_URL

# --- DB CONNECTION ---
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# --- CREATE POSITION ---
def create_position(symbol, side, entry_price, qty, tp_percent, sl_percent):
    fills = [{"qty": qty, "price": entry_price}]
    local_avg = entry_price

    sql = """
        INSERT INTO positions (
            symbol, side, entry_static, fills, local_avg,
            executed, tp_percent, sl_percent, auto_close_enabled,
            last_dca_ts, status
        )
        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, 'active')
        RETURNING id;
    """

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                symbol, side, entry_price, psycopg2.extras.Json(fills),
                local_avg, 0, tp_percent, sl_percent, False, time.time()
            ))
            new_id = cur.fetchone()[0]
            return new_id


# --- LOAD ALL ACTIVE POSITIONS ---
def load_active_positions():
    sql = "SELECT * FROM positions WHERE status = 'active';"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return cur.fetchall()


# --- LOAD SINGLE POSITION ---
def load_position(pos_id):
    sql = "SELECT * FROM positions WHERE id = %s;"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (pos_id,))
            return cur.fetchone()


# --- UPDATE FILLS + LOCAL AVG ---
def add_fill(pos_id, qty, price):
    pos = load_position(pos_id)
    fills = pos["fills"]
    fills.append({"qty": qty, "price": price})

    # local_avg berechnen
    total_qty = sum(float(f["qty"]) for f in fills)
    total_value = sum(float(f["qty"]) * float(f["price"]) for f in fills)
    local_avg = total_value / total_qty

    sql = """
        UPDATE positions
        SET fills = %s::jsonb,
            local_avg = %s,
            updated_at = NOW()
        WHERE id = %s;
    """

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                psycopg2.extras.Json(fills),
                local_avg,
                pos_id
            ))


# --- UPDATE EXECUTED DCA COUNT ---
def update_executed(pos_id, executed):
    sql = """
        UPDATE positions
        SET executed = %s,
            updated_at = NOW()
        WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (executed, pos_id))


# --- ENABLE AUTO CLOSE ---
def enable_auto_close(pos_id):
    sql = """
        UPDATE positions
        SET auto_close_enabled = TRUE,
            updated_at = NOW()
        WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pos_id,))


# --- UPDATE LAST DCA TIMESTAMP ---
def update_last_dca_ts(pos_id):
    sql = """
        UPDATE positions
        SET last_dca_ts = %s,
            updated_at = NOW()
        WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (time.time(), pos_id))


# --- CLOSE POSITION ---
def close_position(pos_id):
    sql = """
        UPDATE positions
        SET status = 'closed',
            updated_at = NOW()
        WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pos_id,))
