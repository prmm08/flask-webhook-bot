# init_db.py
import psycopg
from config import DATABASE_URL

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

DDL = """
CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_static NUMERIC,
    fills JSONB,
    local_avg NUMERIC,
    executed INTEGER DEFAULT 0,
    tp_percent NUMERIC,
    sl_percent NUMERIC,
    auto_close_enabled BOOLEAN DEFAULT FALSE,
    last_dca_ts DOUBLE PRECISION,
    dca_count INTEGER DEFAULT 4,
    dca_deviation_percent NUMERIC DEFAULT 5,
    dca_volume_multiplier NUMERIC DEFAULT 2,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""

def main():
    with psycopg.connect(DATABASE_URL, sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    print("DB initialized")

if __name__ == "__main__":
    main()
