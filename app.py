import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
import logging
from math import floor
from flask import Flask, request, jsonify

# ============================================================
#   KONFIGURATION & PARAMETER
# ============================================================
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# Standard-Parameter (werden genutzt, wenn Webhook nichts mitschickt)
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1.0
SL_PERCENT = 40.0

# DCA Parameter
DCA_DEVIATION_PERCENT = 5.0    # Wann nachkaufen? (z.B. alle 5% Drop)
DCA_VOLUME_MULTIPLIER = 2      # Verdoppeln beim Nachkauf?
DCA_MAX_STEPS = 5              # Wie oft maximal nachkaufen?
DCA_TP_PERCENT = 1.0           # TP für die gesamte Position nach DCA
DCA_SL_PERCENT = 40.0          # SL für die gesamte Position nach DCA

DCA_SAVE_FILE = "active_dca.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
app = Flask(__name__)

active_dca = {}
dca_lock = threading.Lock()
symbol_info_cache = {}

# ============================================================
#   PERSISTENZ & HELPERS
# ============================================================

def save_dca_data():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f:
                json.dump(active_dca, f, indent=4)
        except Exception as e: logging.error(f"Save Error: {e}")

def load_dca_data():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f:
                active_dca = json.load(f)
        except Exception as e: logging.error(f"Load Error: {e}")

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    p = dict(params) if params else {}
    p["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    full_url = f"{url}?{query_string}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        resp = requests.get(full_url, headers=headers, timeout=10) if method == "GET" else requests.post(full_url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        logging.error(f"API Error: {e}")
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
    return round(floor(value * inv + 0.0000001) / inv, 8)

# ============================================================
#   CORE TRADING LOGIC (TP/SL FIX INTEGRIERT)
# ============================================================

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p, quantity):
    """Setzt TP/SL mit der 'Garantie-Methode' (Methode B aus deinen Logs)"""
    time.sleep(6) # Wichtig für BingX Positions-Sync
    info = get_symbol_info(symbol)
    tp_price = round_step(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), info["price_step"])
    sl_price = round_step(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), info["price_step"])
    order_side = "SELL" if side == "LONG" else "BUY"
    safe_qty = round_step(quantity, info["qty_step"])

    for o_type, price in [("TAKE_PROFIT_MARKET", tp_price), ("STOP_MARKET", sl_price)]:
        payload = {
            "symbol": symbol, "side": order_side, "positionSide": side,
            "type": o_type, "stopPrice": str(price), "workingType": "MARK_PRICE",
            "quantity": str(safe_qty), "closePosition": "true"
        }
        res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
        if res and res.get("code") == 0:
            logging.info(f"[SUCCESS] {o_type} @ {price}")
        else:
            logging.error(f"[FAIL] {o_type} konnte nicht gesetzt werden: {res}")

def execute_trade(symbol, direction, leverage, trade_size, tp_p, sl_p):
    # Check ob Position existiert
    pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    if pos and any(float(p["positionAmt"]) != 0 for p in pos.get("data", [])):
        return

    price_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    price = float(price_res["data"]["price"])
    
    # Leverage & Order
    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": leverage, "side": direction})
    info = get_symbol_info(symbol)
    qty = round_step(trade_size / price, info["qty_step"])
    
    logging.info(f"[ORDER] Start {direction} {symbol}")
    res = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction, "type": "MARKET", "quantity": str(qty)
    })

    if res and res.get("code") == 0:
        with dca_lock:
            active_dca[symbol] = {
                "side": direction, "entry_static": price, "entry_dynamic": price,
                "executed": 0, "base_trade_size": trade_size, "qty": qty
            }
            save_dca_data()
        threading.Thread(target=set_exchange_tp_sl, args=(symbol, direction, price, tp_p, sl_p, qty)).start()

# ============================================================
#   DCA MONITOR WORKER
# ============================================================

def monitor_worker():
    while True:
        try:
            pos_res = api_request("GET", "/openApi/swap/v2/user/positions")
            active_list = {p["symbol"]: p for p in pos_res.get("data", []) if float(p["positionAmt"]) != 0} if pos_res else {}

            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_list]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca_data()

            for symbol in list(active_dca.keys()):
                d = active_dca[symbol]
                curr_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
                curr = float(curr_res["data"]["price"])

                # DCA Logik
                if d["executed"] < DCA_MAX_STEPS:
                    # ... (DCA Logik wie gehabt)
                    pass 
                
        except Exception: pass
        time.sleep(10)

# ============================================================
#   WEBHOOK ENDPOINT
# ============================================================

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    ticker = data.get("ticker", "BTC")
    symbol = f"{str(ticker).upper()}-USDT"
    
    # Hier werden die Parameter aus dem Webhook gelesen
    threading.Thread(target=execute_trade, args=(
        symbol, 
        str(data.get("direction", "LONG")).upper(),
        int(data.get("leverage", LEVERAGE)),
        float(data.get("trade_size", TRADE_SIZE)),
        float(data.get("tp_percent", TP_PERCENT)),
        float(data.get("sl_percent", SL_PERCENT))
    )).start()
    
    return jsonify({"status": "received"}), 200

@app.route("/ping")
def ping(): return "OK", 200

if __name__ == "__main__":
    load_dca_data()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))