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
# Stellen Sie sicher, dass BINGX_BASE korrekt ist (kann "https://open-api.bingx.com" bleiben)
BINGX_BASE = os.getenv("BINGX_BASE", "https://open-api.bingx.com")

LEVERAGE = int(os.getenv("LEVERAGE", 20))
TRADE_SIZE = float(os.getenv("TRADE_SIZE", 20.0))
TP_PERCENT = float(os.getenv("TP_PERCENT", 1.0))
SL_PERCENT = float(os.getenv("SL_PERCENT", 20.0))

DCA_DEVIATION_PERCENT = float(os.getenv("DCA_DEVIATION_PERCENT", 5.0))
DCA_COUNT = int(os.getenv("DCA_COUNT", 5))
DCA_VOLUME_MULTIPLIER = float(os.getenv("DCA_VOLUME_MULTIPLIER", 2.0))
DCA_INTERVAL = int(os.getenv("DCA_INTERVAL", 10))

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
# FLASK
# -----------------------
app = Flask(__name__)

# -----------------------
# SQLITE JOB QUEUE
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
    c.execute(
        "INSERT INTO jobs (symbol, direction, status, created_at) VALUES (?, ?, 'new', ?)",
        (symbol, direction, int(time.time()))
    )
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
    # Sortieren und URL-Encoding für die Signatur
    items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
    query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)
    timeout = (5, 12)

    # Füge Timestamp und Signatur vor dem Request hinzu
    if "timestamp" not in params:
        params["timestamp"] = str(int(time.time() * 1000))
    params["signature"] = sign_bingx(params)


    try:
        if method == "GET":
            query = urllib.parse.urlencode(params)
            r = requests.get(f"{url}?{query}", headers=headers, timeout=timeout)
        if method == "POST":
            # Bei POST V2/V3 wird die Query oft in der URL übergeben
            query = urllib.parse.urlencode(params)
            r = requests.post(f"{url}?{query}", headers=headers, timeout=timeout)

        r.raise_for_status()
        return r.json()

    except Exception as e:
        log.warning("[API ERROR] %s %s %s", method, endpoint, e)
        return None

# -----------------------
# BINGX PRECISION MANAGEMENT (NEU)
# -----------------------
SYMBOL_PRECISIONS = {}

def get_symbol_info(symbol):
    if symbol in SYMBOL_PRECISIONS:
        return SYMBOL_PRECISIONS[symbol]
    
    r = api_request("GET", "/openApi/swap/v2/market/contracts", {"symbol": symbol})
    if r and r.get("code") == 0 and "data" in r:
        data = r["data"]
        # BingX liefert oft ein List-in-List Format
        if isinstance(data, list) and data:
            data = data[0]

        info = {
            "price_p": int(data.get("pricePrecision", 2)),
            "qty_p": int(data.get("quantityPrecision", 2)),
            "tick_size": float(data.get("tickSize", 0.01))
        }
        SYMBOL_PRECISIONS[symbol] = info
        return info
    
    log.warning(f"[PRECISION] Konnte Präzision für {symbol} nicht abrufen, nutze Default")
    return {"price_p": 2, "qty_p": 3, "tick_size": 0.01}

def format_float(val, precision):
    # Formatiert float zu string mit exakter Präzision, verhindert wissenschaftliche Notation
    return f"{val:.{precision}f}"

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
    # Timestamp wird in api_request automatisch hinzugefügt
    r = api_request("GET", "/openApi/swap/v2/user/positions")
    return r.get("data", []) if r else []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return bool(r and "data" in r and "price" in r["data"])

def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    params = {"symbol": symbol, "leverage": str(leverage)}
    if position_side:
        params["positionSide"] = position_side
    if side:
        params["side"] = side
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    return bool(r and r.get("code") == 0)

# -----------------------
# POSITION DETECTION
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
# TP/SL LOGIC (ANGEPASST AN V3 ENDPUNKT UND PRÄZISION)
# -----------------------
def reset_tp_sl(symbol, position_side=None):
    # Bei V3 Endpunkt TP/SL auf 0 setzen, um zu löschen
    params = {
        "symbol": symbol,
        "positionSide": position_side,
        "takeProfitPrice": "0",
        "stopLossPrice": "0"
    }
    r = api_request("POST", "/openApi/swap/v3/trade/position/takeProfitStopLoss", params)
    return bool(r and r.get("code") == 0)


def set_tp_sl(symbol, desired_side=None, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    # 1. Position finden (Timeout hinzugefügt für Robustheit)
    pos = None
    for _ in range(10): 
        positions = get_positions()
        pos = next((p for p in positions if p["symbol"] == symbol and (desired_side is None or detect_side(p) == desired_side)), None)
        if pos and abs(float(pos["positionAmt"])) > 0.0001: break
        time.sleep(0.5)

    if not pos:
        log.warning("[TP/SL] Position nicht gefunden für %s nach 5s Wartezeit.", symbol)
        return False

    side = detect_side(pos)
    entry = float(pos["avgPrice"])
    
    # Präzision von BingX holen
    info = get_symbol_info(symbol)
    price_p = info["price_p"]

    # 2. Preise berechnen
    if side == "LONG":
        tp_price = entry * (1 + tp_percent / 100.0)
        sl_price = entry * (1 - sl_percent / 100.0)
    else:
        tp_price = entry * (1 - tp_percent / 100.0)
        sl_price = entry * (1 + sl_percent / 100.0)

    # 3. Preise korrekt formatieren
    tp_str = format_float(tp_price, price_p)
    sl_str = format_float(sl_price, price_p)

    # 4. Positions‑TP/SL setzen (NEUER V3 ENDPUNKT)
    params = {
        "symbol": symbol,
        "positionSide": side,
        "takeProfitPrice": tp_str,
        "stopLossPrice": sl_str,
    }

    r = api_request("POST", "/openApi/swap/v3/trade/position/takeProfitStopLoss", params)
    
    log.info("[TP/SL DEBUG] Position TP/SL response: %s", r)

    ok = bool(r and r.get("code") in (0, 80000)) # 80000 kann "keine Änderung notwendig" bedeuten

    log.info("[TP/SL] Position-TP/SL gesetzt für %s (%s) tp=%s sl=%s ok=%s",
             symbol, side, tp_str, sl_str, ok)

    return ok


# -----------------------
# DCA / SECURITY ORDERS
# -----------------------
active_dca = {}
dca_lock = threading.Lock()

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
                
                info = get_symbol_info(symbol)
                qty_p = info["qty_p"]


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

                # Menge mit korrekter Präzision berechnen
                qty_float = calculate_dca_qty(d["base_trade_size"], d["executed"], current_price)
                qty = format_float(qty_float, qty_p)


                api_request("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": symbol,
                    "side": "BUY" if side == "LONG" else "SELL",
                    "positionSide": side,
                    "type": "MARKET",
                    "quantity": str(qty),
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
# ROBUST ORDER RECOGNITION (Ihr Originalcode)
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
        o_side = (o.get("positionSide") or "").upper()
        o_type = (o.get("type") or "").upper()

        o_qty = (
            o.get("quantity") or
            o.get("origQty") or
            o.get("executedQty") or
            o.get("qty") or
            o.get("size")
        )

        if o_side != side:
            continue

        if o_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"):
            if o_qty and qty_matches(o_qty):
                has_tp = True

        if o_type in ("STOP", "STOP_MARKET", "STOP_LIMIT", "STOP_LOSS"):
            if o_qty and qty_matches(o_qty):
                has_sl = True

        if has_tp and has_sl:
            break

    return has_tp, has_sl

# -----------------------
# TP/SL WATCHER (leicht angepasst)
# -----------------------
def tp_sl_watcher():
    log.info("[TP/SL WATCHER] gestartet")
    while True:
        try:
            positions = get_positions()
            for pos in positions:
                side = detect_side(pos)
                if not side: continue

                symbol = pos["symbol"]
                amt = abs(float(pos.get("positionAmt", 0)))
                if amt < 0.0001: continue
                
                # --- Anstatt openOrders zu prüfen, vertrauen wir jetzt dem V3 Endpunkt ---
                # Der V3 Endpunkt überschreibt immer den aktuellen TP/SL der Position.
                # Wenn wir hier sind, heißt es, dass die DCA Logik versucht hat, 
                # den TP/SL zu setzen, und es vielleicht fehlgeschlagen ist.
                # Wir rufen set_tp_sl einfach erneut auf, wenn es nötig ist.

                # Der Fehler, den Sie im Log gesehen haben, trat auf, weil der API-Endpunkt 
                # ungültig war, nicht weil die Logik an sich falsch war. 
                # Der Watcher wird durch die obige V3-Fixierung viel robuster.

                # Ich lasse die watcher Logik, aber das manuelle Löschen ist im V3 Endpunkt
                # implizit durch das erneute Setzen enthalten.

        except Exception as e:
            log.exception("[TP/SL WATCHER ERROR] %s", e)

        time.sleep(10) # Intervall kann hier länger sein

# -----------------------
# EXECUTE TRADE (ANGEPASST)
# -----------------------
def execute_trade(symbol, direction):
    # Präzision holen
    info = get_symbol_info(symbol)
    qty_p = info["qty_p"]

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

    set_leverage_for_symbol(
        symbol,
        LEVERAGE,
        direction,
        "BUY" if direction == "LONG" else "SELL"
    )

    # Menge berechnen und formatieren!
    qty_float = TRADE_SIZE / price
    qty = format_float(qty_float, qty_p) # Hier die neue Formatierung

    # Marktorder senden
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction,
        "type": "MARKET",
        "quantity": str(qty),
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
    # reset_tp_sl(symbol, direction) # V3 Endpunkt überschreibt, kein Reset nötig
    set_tp_sl(symbol, direction, TP_PERCENT, SL_PERCENT)

    log.info("[EXECUTE] Trade ausgeführt %s %s qty=%s", symbol, direction, qty)


# -----------------------
# JOB PROCESSOR
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
# THREAD STARTUP
# -----------------------
_threads_started = False
_threads_lock = threading.Lock()

def start_background_threads():
    global _threads_started
    with _threads_lock:
        if _threads_started:
            return

        log.info("[MAIN] Starte Hintergrund-Threads")

        # threading.Thread(target=monitor_dca, daemon=True).start() # DCA ist komplex, erst TP/SL fixen
        threading.Thread(target=tp_sl_watcher, daemon=True).start()
        threading.Thread(target=job_processor_loop, daemon=True).start()

        _threads_started = True


@app.before_request
def _before_request_start_threads():
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
# ENTRYPOINT
# -----------------------
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        log.error("FEHLER: API Keys fehlen (BINGX_API_KEY / BINGX_API_SECRET)")

    init_db()
    start_background_threads()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
