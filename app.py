import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
import logging
from flask import Flask, request, jsonify

# --- API ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- SETTINGS ---
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1
SL_PERCENT = 40
DCA_DEVIATION_PERCENT = 5.0 
DCA_VOLUME_MULTIPLIER = 2
DCA_SAVE_FILE = "active_dca.json"

active_dca = {}
processing_symbols = set() # Verhindert Doppel-Orders
dca_lock = threading.Lock()

# ============================================================
#   HELPERS & PERSISTENCE
# ============================================================

def save_dca_data():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f:
                json.dump(active_dca, f)
        except: pass

def load_dca_data():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f:
                active_dca = json.load(f)
        except: pass

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    p = dict(params) if params else {}
    p["timestamp"] = str(int(time.time() * 1000))
    
    query_string = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    full_url = f"{url}?{query_string}&signature={sig}"
    
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        if method == "GET":
            return requests.get(full_url, headers=headers, timeout=10).json()
        return requests.post(full_url, headers=headers, timeout=10).json()
    except Exception as e:
        print(f"[API ERROR] {e}")
        return None

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

def get_positions():
    r = api_request("GET", "/openApi/swap/v2/user/positions")
    return r.get("data", []) if r else []

# ============================================================
#   EXCHANGE TP/SL
# ============================================================

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p):
    # Kurze Pause damit die Position bei BingX registriert ist
    time.sleep(2)
    tp = round(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), 6)
    sl = round(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), 6)
    
    for price, otype in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side, "type": otype, "stopPrice": str(price),
            "workingType": "MARK_PRICE", "closePosition": "true"
        })
        print(f"[EXCHANGE] TP/SL {otype} für {symbol}: {res.get('msg')}")

# ============================================================
#   TRADE EXECUTION (WITH DOUBLE-ORDER PROTECTION)
# ============================================================

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if symbol in processing_symbols:
        return
    
    processing_symbols.add(symbol)
    try:
        # Check API if position exists
        pos = get_positions()
        if any(p["symbol"] == symbol and float(p["positionAmt"]) != 0 for p in pos):
            print(f"[SKIP] Position für {symbol} bereits offen.")
            return

        price = get_price(symbol)
        if not price: return

        # Set Leverage
        api_request("POST", "/openApi/swap/v2/trade/leverage", {
            "symbol": symbol, "leverage": leverage, "side": "BUY" if direction == "LONG" else "SELL"
        })

        # Market Order
        qty = round(trade_size / price, 4)
        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction, "type": "MARKET", "quantity": str(qty)
        })

        if res and res.get("code") == 0:
            print(f"[ORDER] {symbol} {direction} erfolgreich platziert.")
            with dca_lock:
                active_dca[symbol] = {
                    "side": direction, "entry_static": price, "entry_dynamic": price,
                    "executed": 0, "base_trade_size": trade_size
                }
                save_dca_data()
            
            # TP/SL auf Exchange setzen
            set_exchange_tp_sl(symbol, direction, price, tp_percent, sl_percent)
    finally:
        # Kurze Sperre aufrecht erhalten, bis API synchron ist
        time.sleep(5)
        processing_symbols.discard(symbol)

# ============================================================
#   WORKER (DCA & BE)
# ============================================================

def monitor_worker():
    while True:
        try:
            positions = get_positions()
            active_list = {p["symbol"]: p for p in positions if float(p["positionAmt"]) != 0}

            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_list]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca_data()

            for symbol, pos in active_list.items():
                with dca_lock:
                    if symbol not in active_dca: continue
                    d = active_dca[symbol]
                
                side = d["side"]
                curr = get_price(symbol)
                if not curr: continue

                # DCA TRIGGER
                if d["executed"] < 5: # DCA_COUNT
                    triggered = (side == "LONG" and curr <= d["entry_static"] * (1 - DCA_DEVIATION_PERCENT/100)) or \
                                (side == "SHORT" and curr >= d["entry_static"] * (1 + DCA_DEVIATION_PERCENT/100))
                    
                    if triggered:
                        # Exchange TP/SL löschen (Bot übernimmt)
                        r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                        for o in r.get("data", {}).get("orders", []):
                            api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
                        
                        qty = round((d["base_trade_size"] * (2 ** (d["executed"] + 1))) / curr, 4)
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                            "positionSide": side, "type": "MARKET", "quantity": str(qty)
                        })
                        with dca_lock:
                            d["executed"] += 1
                            time.sleep(2)
                            p_now = next((p for p in get_positions() if p["symbol"] == symbol), None)
                            if p_now: d["entry_dynamic"] = float(p_now["avgPrice"])
                            save_dca_data()

                # BE EXIT
                if d["executed"] > 0:
                    if (side == "LONG" and curr >= d["entry_dynamic"]) or (side == "SHORT" and curr <= d["entry_dynamic"]):
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
                            "positionSide": side, "type": "MARKET", "closePosition": "true"
                        })
        except: pass
        time.sleep(5)

# ============================================================
#   FLASK
# ============================================================

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    symbol = f"{str(data.get('currency', '')).upper()}-USDT"
    direction = str(data.get("direction", "")).upper()
    
    if direction in ("LONG", "SHORT"):
        threading.Thread(target=execute_trade, args=(
            symbol, direction, int(data.get("leverage", LEVERAGE)),
            float(data.get("trade_size", TRADE_SIZE)),
            float(data.get("tp_percent", TP_PERCENT)),
            float(data.get("sl_percent", SL_PERCENT))
        )).start()
    return jsonify({"status": "processing"}), 200

if __name__ == "__main__":
    load_dca_data()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
