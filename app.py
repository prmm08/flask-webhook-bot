# app.py
import os
import time
import hmac
import hashlib
import urllib.parse
import threading
import sqlite3
import logging
from flask import Flask, request, jsonify
import requests

# -----------------------
# CONFIG / DEFAULTS
# -----------------------
API_KEY = os.getenv("BINGX_API_KEY", "")
API_SECRET = os.getenv("BINGX_API_SECRET", "")
BINGX_BASE = "https://open-api.bingx.com"

# Defaults (hard-coded as requested)
LEVERAGE = int(os.getenv("LEVERAGE", 20))
TRADE_SIZE = float(os.getenv("TRADE_SIZE", 20.0))
TP_PERCENT = float(os.getenv("TP_PERCENT", 2.0))
SL_PERCENT = float(os.getenv("SL_PERCENT", 40.0))

DCA_DEVIATION_PERCENT = float(os.getenv("DCA_DEVIATION_PERCENT", 5.0))
DCA_COUNT = int(os.getenv("DCA_COUNT", 5))
DCA_VOLUME_MULTIPLIER = float(os.getenv("DCA_VOLUME_MULTIPLIER", 2.0))
DCA_INTERVAL = int(os.getenv("DCA_INTERVAL", 5))

DB_PATH = os.getenv("JOB_DB_PATH", "jobs.db")

# -----------------------
# LOGGING
# -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("bot")

# -----------------------
# STATE
# -----------------------
active_dca = {}
dca_lock = threading.Lock()

# -----------------------
# FLASK APP
# -----------------------
app = Flask(__name__)

# -----------------------
# SQLITE JOB QUEUE (simple)
# -----------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        created_at INTEGER NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def enqueue_job(symbol, direction):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO jobs (symbol, direction, status, created_at) VALUES (?, ?, 'new', ?)",
              (symbol, direction, int(time.time())))
    conn.commit()
    conn.close()

def fetch_new_job():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, symbol, direction FROM jobs WHERE status='new' ORDER BY id LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row

def mark_job_processing(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE jobs SET status='processing' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()

def mark_job_done(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()

# -----------------------
# BINGX API HELPERS
# -----------------------
def sign_bingx(params):
    items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
    query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)
    timeout = (5, 12)
    try:
        if method == "GET":
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))
            signature = sign_bingx(params_for_sign)
            params_for_sign["signature"] = signature
            query = urllib.parse.urlencode(params_for_sign)
            r = requests.get(f"{url}?{query}", headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        if method == "POST":
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))
            signature = sign_bingx(params_for_sign)
            query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params_for_sign.items()))
            r = requests.post(f"{url}?{query}&signature={signature}", headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.error("[API ERROR] %s %s %s", method, endpoint, e)
        return None

# -----------------------
# MARKET HELPERS
# -----------------------
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        return float(r["data"]["price"])
    except:
        return None

def get_positions():
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/user/positions", {"timestamp": ts})
    return r.get("data", []) if r else []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return bool(r and "data" in r and "price" in r["data"])

def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(leverage), "timestamp": ts}
    if position_side:
        params["positionSide"] = position_side
    if side:
        params["side"] = side
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    return bool(r)

# -----------------------
# POSITION DETECTION + DUST FILTER
# -----------------------
def detect_side(pos):
    raw = pos.get("positionSide")
    amt = float(pos.get("positionAmt", 0))
    if abs(amt) < 0.0001:
        return None
    if raw in (None, "", "BOTH"):
        return "LONG" if amt > 0 else "SHORT"
    return raw

# -----------------------
# TP/SL LOGIC
# -----------------------
def correct_tp_sl_for_leverage(entry, tp_percent, sl_percent, leverage, side):
    tp_corrected = tp_percent / max(1, leverage)
    sl_corrected = sl_percent / max(1, leverage)
    if side == "LONG":
        tp_price = entry * (1 + tp_corrected / 100)
        sl_price = entry * (1 - sl_corrected / 100)
    else:
        tp_price = entry * (1 - tp_corrected / 100)
        sl_price = entry * (1 + sl_corrected / 100)
    return tp_price, sl_price

def reset_tp_sl(symbol, position_side=None):
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
    orders = r.get("data", {}).get("orders", []) if r else []
    for order in orders:
        pos_side = order.get("positionSide") or order.get("position")
        if position_side and pos_side != position_side:
            continue
        oid = order.get("orderId")
        if not oid:
            continue
        api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {
            "orderId": oid, "symbol": symbol, "timestamp": str(int(time.time() * 1000))
        })

def set_tp_sl(symbol, desired_side=None, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    pos = None
    for _ in range(8):
        positions = get_positions()
        for p in positions:
            side = detect_side(p)
            if not side:
                continue
            if p["symbol"] == symbol and (desired_side is None or side == desired_side):
                pos = p
                break
        if pos:
            break
        time.sleep(0.5)
    if not pos:
        log.info("[TP/SL] Position nicht gefunden für %s", symbol)
        return
    side = detect_side(pos)
    if not side:
        return
    entry = float(pos["avgPrice"])
    qty = abs(float(pos["positionAmt"]))
    leverage = int(pos.get("leverage", LEVERAGE))
    tp_price, sl_price = correct_tp_sl_for_leverage(entry, tp_percent, sl_percent, leverage, side)
    reset_tp_sl(symbol, side)

    # TAKE PROFIT MARKET (kompensiert)
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "SELL" if side == "LONG" else "BUY",
        "positionSide": side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": f"{tp_price:.6f}",
        "quantity": f"{qty:.6f}",
        "reduceOnly": "true",
        "workingType": "MARK_PRICE",
        "timestamp": str(int(time.time() * 1000))
    })

    # STOP-LIMIT (robuster als STOP_MARKET)
    if side == "LONG":
        trigger = sl_price
        limit = trigger * 0.999
    else:
        trigger = sl_price
        limit = trigger * 1.001

    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "SELL" if side == "LONG" else "BUY",
        "positionSide": side,
        "type": "STOP",
        "stopPrice": f"{trigger:.6f}",
        "price": f"{limit:.6f}",
        "quantity": f"{qty:.6f}",
        "reduceOnly": "true",
        "workingType": "CONTRACT_PRICE",
        "timestamp": str(int(time.time() * 1000))
    })

# -----------------------
# DCA / SECURITY ORDERS
# -----------------------
def update_entry(symbol, side):
    positions = get_positions()
    for p in positions:
        ps = detect_side(p)
        if ps == side and p["symbol"] == symbol:
            return float(p["avgPrice"])
    return None

def calculate_dca_qty(base_trade_size, executed, current_price):
    multiplier = DCA_VOLUME_MULTIPLIER ** (executed + 1)
    return round((base_trade_size * multiplier) / current_price, 6)

def should_trigger_dca(side, current, entry_ref, deviation_percent):
    deviation = deviation_percent / 100.0
    if side == "LONG":
        return current <= entry_ref * (1 - deviation)
    if side == "SHORT":
        return current >= entry_ref * (1 + deviation)
    return False

def monitor_dca():
    log.info("[DCA] Monitor gestartet")
    while True:
        try:
            positions = get_positions()
            for pos in positions:
                side = detect_side(pos)
                if not side:
                    continue
                symbol = pos["symbol"]
                amt = float(pos["positionAmt"])
                if abs(amt) < 0.0001:
                    continue
                current_price = get_price(symbol)
                if not current_price:
                    continue
                with dca_lock:
                    if symbol not in active_dca:
                        active_dca[symbol] = {
                            "side": side,
                            "entry_static": float(pos["avgPrice"]),
                            "entry_dynamic": float(pos["avgPrice"]),
                            "executed": 0,
                            "base_trade_size": TRADE_SIZE,
                            "tp_percent": TP_PERCENT,
                            "sl_percent": SL_PERCENT
                        }
                    d = active_dca[symbol]
                if d["executed"] >= DCA_COUNT:
                    continue
                # use dynamic entry for trigger
                if not should_trigger_dca(side, current_price, d["entry_dynamic"], DCA_DEVIATION_PERCENT):
                    continue
                qty = calculate_dca_qty(d["base_trade_size"], d["executed"], current_price)
                api_request("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": symbol,
                    "side": "BUY" if side == "LONG" else "SELL",
                    "positionSide": side,
                    "type": "MARKET",
                    "quantity": str(qty),
                    "timestamp": str(int(time.time() * 1000))
                })
                with dca_lock:
                    d["executed"] += 1
                    new_entry = update_entry(symbol, side)
                    if new_entry:
                        d["entry_dynamic"] = new_entry
                reset_tp_sl(symbol, side)
                set_tp_sl(symbol, side, TP_PERCENT, SL_PERCENT)
        except Exception as e:
            log.exception("[DCA ERROR] %s", e)
        time.sleep(DCA_INTERVAL)

# -----------------------
# TP/SL WATCHER
# -----------------------
def tp_sl_watcher():
    log.info("[TP/SL WATCHER] gestartet")
    while True:
        try:
            positions = get_positions()
            for pos in positions:
                side = detect_side(pos)
                if not side:
                    continue
                symbol = pos["symbol"]
                amt = float(pos["positionAmt"])
                if abs(amt) < 0.0001:
                    continue
                ts = str(int(time.time() * 1000))
                r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
                orders = r.get("data", {}).get("orders", []) if r else []
                has_tp = any(o.get("type") in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") and o.get("positionSide") == side for o in orders)
                has_sl = any(o.get("type") in ("STOP", "STOP_MARKET") and o.get("positionSide") == side for o in orders)
                if not has_tp or not has_sl:
                    log.info("[TP/SL WATCHER] Setze TP/SL neu für %s (%s)", symbol, side)
                    reset_tp_sl(symbol, side)
                    set_tp_sl(symbol, side, TP_PERCENT, SL_PERCENT)
        except Exception as e:
            log.exception("[TP/SL WATCHER ERROR] %s", e)
        time.sleep(10)

# -----------------------
# EXECUTE TRADE (Webhook only currency + direction)
# -----------------------
def execute_trade(symbol, direction):
    if not symbol_exists(symbol):
        log.info("[EXECUTE] Symbol existiert nicht: %s", symbol)
        return
    positions = get_positions()
    for p in positions:
        side = detect_side(p)
        if side == direction and p["symbol"] == symbol and abs(float(p["positionAmt"])) > 0.0001:
            log.info("[EXECUTE] Position bereits offen: %s %s", symbol, direction)
            return
    price = get_price(symbol)
    if not price:
        log.info("[EXECUTE] Kein Preis für %s", symbol)
        return
    set_leverage_for_symbol(symbol, LEVERAGE, direction, "BUY" if direction == "LONG" else "SELL")
    qty = round(TRADE_SIZE / price, 6)
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction,
        "type": "MARKET",
        "quantity": str(qty),
        "timestamp": str(int(time.time() * 1000))
    })
    with dca_lock:
        active_dca[symbol] = {
            "side": direction,
            "entry_static": price,
            "entry_dynamic": price,
            "executed": 0,
            "base_trade_size": TRADE_SIZE,
            "tp_percent": TP_PERCENT,
            "sl_percent": SL_PERCENT
        }
    time.sleep(2)
    reset_tp_sl(symbol, direction)
    set_tp_sl(symbol, direction, TP_PERCENT, SL_PERCENT)
    log.info("[EXECUTE] Trade ausgeführt %s %s qty=%s", symbol, direction, qty)

# -----------------------
# JOB PROCESSOR (polls sqlite queue)
# -----------------------
def job_processor_loop(poll_interval=2):
    log.info("[WORKER] Job Processor gestartet")
    while True:
        row = fetch_new_job()
        if row:
            job_id, symbol, direction = row
            try:
                mark_job_processing(job_id)
                log.info("[WORKER] Verarbeite Job %s: %s %s", job_id, symbol, direction)
                execute_trade(symbol, direction)
                mark_job_done(job_id)
                log.info("[WORKER] Job %s erledigt", job_id)
            except Exception as e:
                log.exception("[WORKER ERROR] %s", e)
        else:
            time.sleep(poll_interval)

# -----------------------
# THREAD STARTUP (safe for Gunicorn single-worker)
# -----------------------
_threads_started = False
_threads_lock = threading.Lock()

def start_background_threads():
    global _threads_started
    with _threads_lock:
        if _threads_started:
            return
        log.info("[MAIN] Starte Hintergrund-Threads")
        threading.Thread(target=monitor_dca, daemon=True).start()
        threading.Thread(target=tp_sl_watcher, daemon=True).start()
        threading.Thread(target=job_processor_loop, daemon=True).start()
        _threads_started = True

# Start threads when the first request arrives (works with Gunicorn single worker)
@app.before_request
def _before_request_start_threads():
    # init_db kann idempotent sein, also sicher hier aufzurufen
    init_db()
    start_background_threads()


# -----------------------
# FLASK ENDPOINTS
# -----------------------
@app.route("/trade", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()
    if not currency or direction not in ("LONG", "SHORT"):
        return jsonify({"status": "ignored"}), 200
    symbol = f"{currency}-USDT"
    enqueue_job(symbol, direction)
    log.info("[WEBHOOK] Job enqueued %s %s", symbol, direction)
    return jsonify({"status": "accepted"}), 200

@app.route("/ping")
def ping():
    return "pong", 200

# -----------------------
# ENTRYPOINT (for python app.py)
# -----------------------
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        log.error("FEHLER: API Keys fehlen (BINGX_API_KEY / BINGX_API_SECRET)")
    init_db()
    # start threads immediately when running with python app.py
    start_background_threads()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
