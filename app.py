import os
import time
import hmac
import hashlib
import urllib.parse
import threading
import sqlite3
import logging
import math
from flask import Flask, request, jsonify
import requests

# -----------------------
# CONFIG
# -----------------------
API_KEY = os.getenv("BINGX_API_KEY", "")
API_SECRET = os.getenv("BINGX_API_SECRET", "")
BINGX_BASE = "https://open-api.bingx.com"

LEVERAGE = int(os.getenv("LEVERAGE", 20))
TRADE_SIZE = float(os.getenv("TRADE_SIZE", 20.0))
TP_PERCENT = float(os.getenv("TP_PERCENT", 1.0))
SL_PERCENT = float(os.getenv("SL_PERCENT", 20.0))

DCA_DEVIATION_PERCENT = float(os.getenv("DCA_DEVIATION_PERCENT", 5.0))
DCA_COUNT = int(os.getenv("DCA_COUNT", 5))
DCA_VOLUME_MULTIPLIER = float(os.getenv("DCA_VOLUME_MULTIPLIER", 2.0))
DCA_INTERVAL = int(os.getenv("DCA_INTERVAL", 10))

DB_PATH = os.getenv("JOB_DB_PATH", "jobs.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

app = Flask(__name__)

# -----------------------
# BINGX PRECISION HELPER
# -----------------------
SYMBOL_PRECISIONS = {}

def get_symbol_info(symbol):
    if symbol in SYMBOL_PRECISIONS:
        return SYMBOL_PRECISIONS[symbol]
    
    # Nutze den Contracts-Endpunkt für Präzisionsdaten
    r = api_request("GET", "/openApi/swap/v2/market/contracts", {"symbol": symbol})
    if r and r.get("code") == 0 and "data" in r:
        data = r["data"]
        # Falls es eine Liste ist, nimm das erste Element
        if isinstance(data, list): data = data[0]
            
        info = {
            "price_p": int(data.get("pricePrecision", 2)),
            "qty_p": int(data.get("quantityPrecision", 2)),
            "tick_size": float(data.get("tickSize", 0.1))
        }
        SYMBOL_PRECISIONS[symbol] = info
        return info
    return {"price_p": 2, "qty_p": 3, "tick_size": 0.01}

def format_float(val, precision):
    return f"{val:.{precision}f}"

# -----------------------
# API & DB CORE (Standard-Logik)
# -----------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, direction TEXT, status TEXT DEFAULT 'new', created_at INTEGER)")
    conn.commit()
    conn.close()

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    p = dict(params) if params else {}
    if "timestamp" not in p: p["timestamp"] = str(int(time.time() * 1000))
    p["signature"] = sign_bingx(p)
    
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        if method == "GET":
            r = requests.get(url, params=p, headers=headers, timeout=10)
        else:
            # POST Parameter müssen oft in der URL übertragen werden bei BingX V2
            query = urllib.parse.urlencode(sorted(p.items()))
            r = requests.post(f"{url}?{query}", headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"API Error {endpoint}: {e}")
        return None

# -----------------------
# TRADING LOGIC (CORRECTED)
# -----------------------
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

def set_tp_sl(symbol, side, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    # 1. Präzision holen
    info = get_symbol_info(symbol)
    
    # 2. Position suchen
    pos = None
    for _ in range(10):
        positions = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
        if positions and "data" in positions:
            for p in positions["data"]:
                if p["symbol"] == symbol and p["positionSide"] == side:
                    pos = p
                    break
        if pos and abs(float(pos["positionAmt"])) > 0: break
        time.sleep(1)

    if not pos: return False

    entry = float(pos["avgPrice"])
    
    # 3. Preise berechnen
    if side == "LONG":
        tp = entry * (1 + tp_percent / 100)
        sl = entry * (1 - sl_percent / 100)
    else:
        tp = entry * (1 - tp_percent / 100)
        sl = entry * (1 + sl_percent / 100)

    # 4. Senden mit korrekter Präzision
    params = {
        "symbol": symbol,
        "positionSide": side,
        "takeProfit": format_float(tp, info["price_p"]),
        "stopLoss": format_float(sl, info["price_p"]),
    }
    
    res = api_request("POST", "/openApi/swap/v2/trade/setPositionTpSl", params)
    log.info(f"[TP/SL] Result for {symbol}: {res}")
    return res and res.get("code") == 0

def execute_trade(symbol, direction):
    info = get_symbol_info(symbol)
    price = get_price(symbol)
    if not price: return

    # Leverage setzen
    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": LEVERAGE, "side": "LONG" if direction == "LONG" else "SHORT"})

    # Menge berechnen & formatieren
    qty = format_float(TRADE_SIZE / price, info["qty_p"])

    # Order platzieren
    order = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction,
        "type": "MARKET",
        "quantity": qty
    })

    if order and order.get("code") == 0:
        log.info(f"[ORDER] Success {symbol} {direction}")
        time.sleep(2) # Warten bis Position im System
        set_tp_sl(symbol, direction)

# -----------------------
# FLASK & BACKGROUND (Rest bleibt gleich)
# -----------------------
@app.route("/trade", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data: return jsonify({"status": "error"}), 400
    symbol = f"{data['currency'].upper()}-USDT"
    direction = data['direction'].upper()
    
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO jobs (symbol, direction, created_at) VALUES (?, ?, ?)", (symbol, direction, int(time.time())))
    conn.commit()
    conn.close()
    return jsonify({"status": "enqueued"}), 200

def worker():
    while True:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT id, symbol, direction FROM jobs WHERE status='new' LIMIT 1").fetchone()
        if row:
            jid, sym, side = row
            conn.execute("UPDATE jobs SET status='done' WHERE id=?", (jid,))
            conn.commit()
            execute_trade(sym, side)
        conn.close()
        time.sleep(2)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
