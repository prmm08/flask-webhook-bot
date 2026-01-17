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

LEVERAGE = int(os.getenv("LEVERAGE", 20))
TRADE_SIZE = float(os.getenv("TRADE_SIZE", 20.0))
TP_PERCENT = float(os.getenv("TP_PERCENT", 1.0))
SL_PERCENT = float(os.getenv("SL_PERCENT", 20.0))

DCA_DEVIATION_PERCENT = float(os.getenv("DCA_DEVIATION_PERCENT", 5.0))
DCA_COUNT = int(os.getenv("DCA_COUNT", 5))
DCA_VOLUME_MULTIPLIER = float(os.getenv("DCA_VOLUME_MULTIPLIER", 2.0))
DCA_INTERVAL = int(os.getenv("DCA_INTERVAL", 5))

DB_PATH = os.getenv("JOB_DB_PATH", "jobs.db")

# -----------------------
# LOGGING
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
# BINGX API HELPERS (improved logging)
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
            try:
                r.raise_for_status()
            except Exception:
                log.warning("[API] GET %s returned %s: %s", endpoint, r.status_code, r.text)
                r.raise_for_status()
            return r.json()
        if method == "POST":
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))
            signature = sign_bingx(params_for_sign)
            query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params_for_sign.items()))
            r = requests.post(f"{url}?{query}&signature={signature}", headers=headers, timeout=timeout)
            try:
                r.raise_for_status()
            except Exception:
                log.warning("[API] POST %s returned %s: %s", endpoint, r.status_code, r.text)
                r.raise_for_status()
            return r.json()
    except requests.exceptions.HTTPError as he:
        log.exception("[API ERROR HTTP] %s %s %s", method, endpoint, he)
        return None
    except Exception as e:
        log.exception("[API ERROR] %s %s %s", method, endpoint, e)
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
# TP/SL LOGIC (set returns success)
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
        return False

    side = detect_side(pos)
    if not side:
        return False

    entry = float(pos["avgPrice"])
    qty = abs(float(pos["positionAmt"]))
    leverage = int(pos.get("leverage", LEVERAGE))
    tp_price, sl_price = correct_tp_sl_for_leverage(entry, tp_percent, sl_percent, leverage, side)

    # cancel existing reduceOnly orders for this side
    try:
        reset_tp_sl(symbol, side)
    except Exception as e:
        log.exception("[TP/SL] Fehler beim Cancel vor Set: %s", e)

    # TAKE PROFIT MARKET
    tp_params = {
        "symbol": symbol,
        "side": "SELL" if side == "LONG" else "BUY",
        "positionSide": side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": f"{tp_price:.6f}",
        "quantity": f"{qty:.6f}",
        "reduceOnly": "true",
        "workingType": "MARK_PRICE",
        "timestamp": str(int(time.time() * 1000))
    }
    r_tp = api_request("POST", "/openApi/swap/v2/trade/order", tp_params)
    if not r_tp or (isinstance(r_tp, dict) and r_tp.get("code") not in (0, None) and r_tp.get("success") not in (True, None)):
        log.warning("[TP/SL] TAKE_PROFIT_MARKET failed for %s: %s", symbol, r_tp)
        tp_ok = False
    else:
        tp_ok = True

    # STOP-LIMIT
    if side == "LONG":
        trigger = sl_price
        limit = trigger * 0.999
    else:
        trigger = sl_price
        limit = trigger * 1.001

    sl_params = {
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
    }
    r_sl = api_request("POST", "/openApi/swap/v2/trade/order", sl_params)
    if not r_sl or (isinstance(r_sl, dict) and r_sl.get("code") not in (0, None) and r_sl.get("success") not in (True, None)):
        log.warning("[TP/SL] STOP (limit) failed for %s: %s", symbol, r_sl)
        sl_ok = False
    else:
        sl_ok = True

    log.info("[TP/SL] Set result for %s side=%s tp_ok=%s sl_ok=%s", symbol, side, tp_ok, sl_ok)
    return tp_ok and sl_ok

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
# ROBUST ORDER RECOGNITION FOR WATCHER
# -----------------------
def orders_have_tp_sl(orders, side, qty, qty_tolerance=0.002):
    def qty_matches(o_qty):
        try:
            o_qty = float(o_qty)
            return abs(o_qty - qty) <= max(qty * qty_tolerance, 1e-8)
        except:
            return False

    has_tp = False
    has_sl = False

    for o in orders:
        # Normalize fields
        o_side = (o.get("positionSide") or "").upper()
        o_type = (o.get("type") or "").upper()
        o_reduce = str(o.get("reduceOnly", "")).lower() in ("true", "1")
        o_qty = (
            o.get("quantity") or
            o.get("origQty") or
            o.get("executedQty") or
            o.get("qty") or
            o.get("size")
        )

        # Only reduce-only orders count
        if not o_reduce:
            continue

        # Only exact side counts
        if o_side != side:
            continue

        # TP detection
        if o_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"):
            if qty_matches(o_qty):
                has_tp = True

        # SL detection
        if o_type in ("STOP", "STOP_MARKET", "STOP_LIMIT", "STOP_LOSS"):
            if qty_matches(o_qty):
                has_sl = True

    return has_tp, has_sl



# -----------------------
# TP/SL WATCHER (robust with backoff)
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
                amt = abs(float(pos.get("positionAmt", 0)))
                if amt < 0.0001:
                    continue

                ts = str(int(time.time() * 1000))
                r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
                orders = r.get("data", {}).get("orders", []) if r else []

                has_tp, has_sl = orders_have_tp_sl(orders, side, amt)

                if not has_tp or not has_sl:
                    log.info("[TP/SL WATCHER] TP/SL fehlen für %s (%s) has_tp=%s has_sl=%s", symbol, side, has_tp, has_sl)
                    try:
                        reset_tp_sl(symbol, side)
                    except Exception as e:
                        log.exception("[TP/SL WATCHER] Fehler beim canceln: %s", e)

                    # Exponential backoff retries
                    max_attempts = 5
                    base_sleep = 1.0
                    success = False
                    for attempt in range(1, max_attempts + 1):
                        ok = set_tp_sl(symbol, side, TP_PERCENT, SL_PERCENT)
                        if ok:
                            success = True
                            log.info("[TP/SL WATCHER] TP/SL erfolgreich gesetzt für %s (%s) nach %s Versuchen", symbol, side, attempt)
                            break
                        else:
                            sleep_time = base_sleep * (2 ** (attempt - 1))
                            log.warning("[TP/SL WATCHER] Versuch %s für %s fehlgeschlagen, warte %.1fs und retry", attempt, symbol, sleep_time)
                            time.sleep(sleep_time)
                    if not success:
                        log.warning("[TP/SL WATCHER] Konnte TP/SL nicht zuverlässig setzen für %s (%s) nach %s Versuchen", symbol, side, max_attempts)
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
# THREAD STARTUP (safe guard)
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

# Use before_request with guard for environments where before_first_request isn't available
@app.before_request
def _before_request_start_threads():
    init_db()
    start_background_threads()

# -----------------------
# FLASK ENDPOINTS
# -----------------------
@app.route("/testorder", methods=["POST"])
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
    start_background_threads()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
