import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
import sys
import traceback
from math import floor
from flask import Flask, request, jsonify

# ============================================================
#   DEBUG HELPER
# ============================================================
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ============================================================
#   KONFIGURATION
# ============================================================
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1.0
SL_PERCENT = 40.0
DCA_DEVIATION_PERCENT = 5.0
DCA_VOLUME_MULTIPLIER = 2
DCA_MAX_STEPS = 5
DCA_TP_PERCENT = 1.2
DCA_SL_PERCENT = 10.0
DCA_SAVE_FILE = "active_dca.json"

app = Flask(__name__)
active_dca = {}

# --- WICHTIGER FIX: RLock statt Lock (Verhindert Deadlock) ---
dca_lock = threading.RLock() 

symbol_info_cache = {}
processing_symbols = set()

# ============================================================
#   API CORE
# ============================================================
def get_sign(params):
    params["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={sig}"

def api_request(method, endpoint, payload=None):
    if payload is None: payload = {}
    query_with_sig = get_sign(payload)
    url = f"{BINGX_BASE}{endpoint}?{query_with_sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        log(f"!!! NETZWERK FEHLER: {e}")
        return None

def get_symbol_info(symbol):
    if symbol in symbol_info_cache: return symbol_info_cache[symbol]
    r = api_request("GET", "/openApi/swap/v2/quote/contracts")
    if r and "data" in r:
        for item in r["data"]:
            if item["symbol"] == symbol:
                p_p = 1 / (10 ** float(item.get("pricePrecision", 4)))
                q_p = 1 / (10 ** float(item.get("quantityPrecision", 2)))
                symbol_info_cache[symbol] = {"price_step": p_p, "qty_step": q_p}
                return symbol_info_cache[symbol]
    return {"price_step": 0.0001, "qty_step": 0.0001}

def round_step(value, step):
    if not step or step == 0: return value
    inv = 1.0 / step
    return round(floor(value * inv + 0.00000001) / inv, 8)

# ============================================================
#   PERSISTENZ
# ============================================================
def save_dca():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f: json.dump(active_dca, f, indent=4)
        except: pass

def load_dca():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f: active_dca = json.load(f)
        except: pass

# ============================================================
#   TP/SL THREAD (CRASH SAFE)
# ============================================================
def set_exchange_tp_sl_safe(symbol, side, entry, tp_p, sl_p, quantity):
    try:
        log(f">>> TP/SL THREAD GESTARTET für {symbol}")
        
        # 1. WARTEN
        log(f"... Warte 5s auf API Sync ({symbol})")
        time.sleep(5)

        # 2. LOGIK
        info = get_symbol_info(symbol)
        tp_price = round_step(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), info["price_step"])
        sl_price = round_step(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), info["price_step"])
        
        close_side = "SELL" if side == "LONG" else "BUY"
        str_qty = str(round_step(quantity, info["qty_step"]))

        orders = [("TAKE_PROFIT_MARKET", tp_price), ("STOP_MARKET", sl_price)]

        for o_type, price in orders:
            # Das Payload Format aus deinem erfolgreichen Test
            payload = {
                "symbol": symbol,
                "side": close_side,
                "positionSide": side,
                "type": o_type,
                "stopPrice": str(price),
                "workingType": "MARK_PRICE",
                "quantity": str_qty,
                "closePosition": "true"
            }
            
            log(f"Sende {o_type}: {price}")
            
            for i in range(3):
                res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
                
                if res and res.get("code") == 0:
                    log(f"   [OK] {o_type} erfolgreich!")
                    break
                else:
                    log(f"   [RETRY] {res.get('msg')}")
                    time.sleep(2)

        log(f"<<< TP/SL THREAD BEENDET für {symbol}")

    except Exception:
        log("!!! CRASH IM TP/SL THREAD !!!")
        traceback.print_exc()

# ============================================================
#   MAIN EXECUTION
# ============================================================
def execute_trade(symbol, direction, leverage, trade_size, tp_p, sl_p):
    if symbol in processing_symbols: return
    processing_symbols.add(symbol)
    
    try:
        log(f"--- NEUER TRADE: {symbol} {direction} ---")
        
        price_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
        if not price_res or "data" not in price_res: return
        price = float(price_res["data"]["price"])
        log(f"Preis: {price}")

        api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": leverage, "side": direction})
        
        info = get_symbol_info(symbol)
        qty = round_step(trade_size / price, info["qty_step"])
        
        log(f"Öffne Market Order: Qty {qty}")
        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction, "type": "MARKET", "quantity": str(qty)
        })

        log(f"ORDER ANTWORT: {json.dumps(res)}")

        if res and res.get("code") == 0:
            # --- HIER WAR DER FEHLER (DEADLOCK) ---
            with dca_lock:
                active_dca[symbol] = {
                    "side": direction, "entry_static": price, "entry_dynamic": price,
                    "executed": 0, "base_trade_size": trade_size, "qty": qty
                }
                save_dca() # Dank RLock klappt das jetzt!
            
            # Jetzt wird dieser Thread auch wirklich gestartet
            t = threading.Thread(target=set_exchange_tp_sl_safe, args=(symbol, direction, price, tp_p, sl_p, qty))
            t.start()
        else:
            log(f"Order fehlgeschlagen! Kein TP/SL Versuch.")

    except Exception:
        log("!!! CRASH IN EXECUTE_TRADE !!!")
        traceback.print_exc()
    finally:
        time.sleep(2)
        processing_symbols.discard(symbol)

# ============================================================
#   WORKER & SERVER
# ============================================================
def monitor_worker():
    while True:
        try:
            # (DCA Logik hier...)
            pass 
        except: pass
        time.sleep(10)

@app.route("/ping")
def ping(): return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    raw = data.get("ticker") or data.get("currency") or data.get("pair") or data.get("symbol")
    if not raw: return jsonify({"error": "No ticker"}), 400
    
    symbol = str(raw).upper().replace("USDT", "").replace("-", "") + "-USDT"
    
    threading.Thread(target=execute_trade, args=(
        symbol, str(data.get("direction", "LONG")).upper(),
        int(data.get("leverage", LEVERAGE)), float(data.get("trade_size", TRADE_SIZE)),
        float(data.get("tp_percent", TP_PERCENT)), float(data.get("sl_percent", SL_PERCENT))
    )).start()
    return jsonify({"status": "processing"}), 200

if __name__ == "__main__":
    load_dca()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))