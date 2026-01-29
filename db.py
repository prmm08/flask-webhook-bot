# db.py
import os
import time
import json

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

# Try psycopg (psycopg3) first, fall back to psycopg2 if needed
_db_driver = None
try:
    import psycopg  # psycopg3
    _db_driver = "psycopg"
except Exception:
    try:
        import psycopg2
        import psycopg2.extras
        _db_driver = "psycopg2"
    except Exception:
        raise RuntimeError("Neither psycopg (psycopg3) nor psycopg2 is installed")


# -------------------------
# Connection helper
# -------------------------
def get_conn():
    """
    Returns a new DB connection. Uses sslmode=require for Render/Postgres.
    Caller should use context manager 'with get_conn() as conn:'.
    """
    if _db_driver == "psycopg":
        # psycopg.connect returns a connection that can be used as context manager
        return psycopg.connect(DATABASE_URL, autocommit=False, sslmode="require")
    else:
        # psycopg2
        return psycopg2.connect(DATABASE_URL, sslmode="require")


# -------------------------
# Utility: recalc local avg from fills list
# -------------------------
def _recalc_local_avg_from_fills(fills):
    """
    fills: list of {"qty": <num>, "price": <num>}
    returns: float local_avg or None if no qty
    """
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


# -------------------------
# Create position
# -------------------------
def create_position(symbol, side, entry_price, qty, tp_percent, sl_percent):
    """
    Inserts a new position row and returns the new id.
    fills is initialized with the initial fill (qty, entry_price).
    """
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

    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    symbol, side, entry_price, json.dumps(fills),
                    local_avg, 0, tp_percent, sl_percent, False, last_dca_ts
                ))
                new_id = cur.fetchone()[0]
                conn.commit()
                return new_id
    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    symbol, side, entry_price, psycopg2.extras.Json(fills),
                    local_avg, 0, tp_percent, sl_percent, False, last_dca_ts
                ))
                new_id = cur.fetchone()[0]
                conn.commit()
                return new_id


# -------------------------
# Load active positions
# -------------------------
def load_active_positions():
    """
    Returns a list of dicts for positions with status='active'.
    Each dict maps column names to values; fills is parsed as Python list.
    """
    sql = "SELECT * FROM positions WHERE status = 'active';"
    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                # ensure fills is a Python list
                for r in rows:
                    if isinstance(r.get("fills"), str):
                        r["fills"] = json.loads(r["fills"])
                return rows
    else:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                return rows


# -------------------------
# Load single position by id
# -------------------------
def load_position(pos_id):
    sql = "SELECT * FROM positions WHERE id = %s;"
    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(sql, (pos_id,))
                r = cur.fetchone()
                if r and isinstance(r.get("fills"), str):
                    r["fills"] = json.loads(r["fills"])
                return r
    else:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (pos_id,))
                return cur.fetchone()


# -------------------------
# Add fill and recalc local_avg
# -------------------------
def add_fill(pos_id, qty, price):
    """
    Appends a fill to fills JSONB, recalculates local_avg and updates the row.
    """
    pos = load_position(pos_id)
    if not pos:
        raise ValueError("Position not found")

    fills = pos.get("fills") or []
    # ensure fills is a list
    if isinstance(fills, str):
        fills = json.loads(fills)

    fills.append({"qty": qty, "price": price})
    local_avg = _recalc_local_avg_from_fills(fills)
    if local_avg is None:
        local_avg = pos.get("local_avg") or 0.0

    sql = """
        UPDATE positions
        SET fills = %s::jsonb,
            local_avg = %s,
            updated_at = NOW()
        WHERE id = %s;
    """

    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (json.dumps(fills), local_avg, pos_id))
                conn.commit()
    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (psycopg2.extras.Json(fills), local_avg, pos_id))
                conn.commit()


# -------------------------
# Update executed count
# -------------------------
def update_executed(pos_id, executed):
    sql = """
        UPDATE positions
        SET executed = %s,
            updated_at = NOW()
        WHERE id = %s;
    """
    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (executed, pos_id))
                conn.commit()
    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (executed, pos_id))
                conn.commit()


# -------------------------
# Enable auto close
# -------------------------
def enable_auto_close(pos_id):
    sql = """
        UPDATE positions
        SET auto_close_enabled = TRUE,
            updated_at = NOW()
        WHERE id = %s;
    """
    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (pos_id,))
                conn.commit()
    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (pos_id,))
                conn.commit()


# -------------------------
# Update last_dca_ts
# -------------------------
def update_last_dca_ts(pos_id):
    sql = """
        UPDATE positions
        SET last_dca_ts = %s,
            updated_at = NOW()
        WHERE id = %s;
    """
    ts = time.time()
    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (ts, pos_id))
                conn.commit()
    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (ts, pos_id))
                conn.commit()


# -------------------------
# Close position (mark as closed)
# -------------------------
def close_position(pos_id):
    sql = """
        UPDATE positions
        SET status = 'closed',
            updated_at = NOW()
        WHERE id = %s;
    """
    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (pos_id,))
                conn.commit()
    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (pos_id,))
                conn.commit()


# -------------------------
# Optional helper: list all positions (for debugging)
# -------------------------
def list_all_positions(limit=100):
    sql = "SELECT * FROM positions ORDER BY created_at DESC LIMIT %s;"
    if _db_driver == "psycopg":
        with get_conn() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(sql, (limit,))
                rows = cur.fetchall()
                for r in rows:
                    if isinstance(r.get("fills"), str):
                        r["fills"] = json.loads(r["fills"])
                return rows
    else:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (limit,))
                return cur.fetchall()
