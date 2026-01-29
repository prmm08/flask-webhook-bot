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

# --- DEFAULT SETTINGS ---
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1
SL_PERCENT = 40

# --- DCA SETTINGS ---
DCA_INTERVAL = 5
DCA_COUNT = 4
DCA_DEVIATION_PERCENT = 5.0
DCA_VOLUME_MULTIPLIER = 2
DCA_SAVE_FILE = "active_dca.json"

active_dca = {}
dca_lock = threading.Lock()
last_dca_heartbeat = time.time()

# ============================================================
#   PERSISTENCE (SPEICHER-FUNKTION)
# ============================================================

def save_dca_data():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f:
                json.dump(active_dca, f)
        except Exception as e:
            print("[SAVE ERROR]", e)

def load_dca_data():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f:
                active_dca = json.load(f)
            print(f"[LOAD] {len(active_dca)} Positionen geladen.")
        except Exception as e:
            print("[LOAD ERROR]", e)

# ============================================================
#   BINGX API HELPERS
# ============================================================

def sign_bingx(params):
    if not params:
        query_string = ""
    else:
        items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
        query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)
    timeout = (5, 10)

    if method == "GET":
        try:
            params_for_sign = dict(params)
            signature = sign_bingx(params_for_sign)
            params_for_sign["signature"] = signature
            query = urllib.parse.urlencode(params_for_sign)
            response = requests.get(f"{url}?{query}", headers=headers, timeout=timeout)
            return response.json()
        except Exception as e:
            print("[API ERROR GET]", e)
            return None

    if method == "POST":
        try:
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))
            query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params_for_sign.items()))
            signature = sign_bingx(params_for_sign)
            response = requests.post(f"{url}?{query}&signature={signature}", headers=headers, timeout=timeout)
            return response.json()
        except Exception as e:
            print("[API ERROR POST]", e)
            return None

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    try: return float(r["data"]["price"])
    except: return None

def get_positions():
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/user/positions", {"timestamp": ts})
    return r.get("data", []) if r else []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return r and "data" in r and "price" in r["data"]

def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(leverage), "timestamp": ts}
    if position_side: params["positionSide"] = position_side
    if side: params["side"] = side
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    return bool(r)

# ============================================================
#   TP / SL LOGIC
# ============================================================

def reset_tp_sl(symbol, position_side=None):
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
    orders = r.get("data", {}).get("orders", []) if r else []
    for order in orders:
        pos_side = order.get("positionSide") or order.get("position")
        if position_side and pos_side != position_side: continue
        oid = order.get("orderId")
        if oid:
            api_request("POST", "/openApi/swap/v2/trade/cancelOrder",
                        {"orderId": oid, "symbol": symbol, "timestamp": str(int(time.time() * 1000))})

def set_tp_sl(symbol, desired_side=None, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    pos = None
    for _ in range(8):
        positions = get_positions()
        pos = next((p for p in positions if p["symbol"] == symbol and float(p.get("positionAmt", 0)) != 0
                    and (desired_side is None or p.get("positionSide") == desired_side)), None)
        if pos: break
        time.sleep(1)
    if not pos: return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])
    tp = entry * (1 + tp_percent / 100) if side == "LONG" else entry * (1 - tp_percent / 100)
    sl = entry * (1 - sl_percent / 100) if side == "LONG" else entry * (1 + sl_percent / 100)
    
    reset_tp_sl(symbol, side)
    for price, otype in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side, "type": otype, "stopPrice": f"{price:.6f}",
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": str(int(time.time() * 1000))
        })

# ============================================================
#   DCA ENGINE
# ============================================================

def monitor_dca():
    global last_dca_heartbeat
    while True:
        last_dca_heartbeat = time.time()
        try:
            positions = get_positions()
            active_symbols_in_api = [p["symbol"] for p in positions if float(p["positionAmt"]) != 0]

            # Cleanup local data
            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_symbols_in_api]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca_data()

            for pos in positions:
                symbol, side, amt = pos["symbol"], pos["positionSide"], float(pos["positionAmt"])
                if amt == 0: continue
                current_price = get_price(symbol)
                if not current_price: continue

                with dca_lock:
                    if symbol not in active_dca:
                        active_dca[symbol] = {
                            "side": side, "entry_static": float(pos["avgPrice"]),
                            "entry_dynamic": float(pos["avgPrice"]), "executed": 0,
                            "base_trade_size": abs(amt) * float(pos["avgPrice"]),
                            "tp_percent": TP_PERCENT, "sl_percent": SL_PERCENT
                        }
                    d = active_dca[symbol]

                if d["executed"] < DCA_COUNT:
                    triggered = (side == "LONG" and current_price <= d["entry_static"] * (1 - DCA_DEVIATION_PERCENT/100)) or \
                                (side == "SHORT" and current_price >= d["entry_static"] * (1 + DCA_DEVIATION_PERCENT/100))
                    
                    if triggered:
                        qty = round((d["base_trade_size"] * (DCA_VOLUME_MULTIPLIER ** (d["executed"] + 1))) / current_price, 6)
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                            "positionSide": side, "type": "MARKET", "quantity": str(qty)
                        })
                        with dca_lock:
                            d["executed"] += 1
                            time.sleep(1)
                            new_pos = next((p for p in get_positions() if p["symbol"] == symbol and p["positionSide"] == side), None)
                            if new_pos: d["entry_dynamic"] = float(new_pos["avgPrice"])
                            save_dca_data()
                        set_tp_sl(symbol, side, d["tp_percent"], d["sl_percent"])

        except Exception as e: print("[DCA ERROR]", e)
        time.sleep(DCA_INTERVAL)

# ============================================================
#   BREAK EVEN WATCHER (NEW)
# ============================================================

def break_even_watcher():
    while True:
        try:
            with dca_lock:
                items = list(active_dca.items())
            for symbol, data in items:
                if data["executed"] > 0:
                    curr = get_price(symbol)
                    if not curr: continue
                    be = data["entry_dynamic"]
                    if (data["side"] == "LONG" and curr >= be) or (data["side"] == "SHORT" and curr <= be):
                        print(f"[BE EXIT] {symbol} erreicht.")
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "SELL" if data["side"] == "LONG" else "BUY",
                            "positionSide": data["side"], "type": "MARKET", "closePosition": "true"
                        })
                        with dca_lock:
                            if symbol in active_dca: del active_dca[symbol]
                            save_dca_data()
        except Exception as e: print("[BE ERROR]", e)
        time.sleep(2)

# ============================================================
#   TRADING & FLASK
# ============================================================

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if not symbol_exists(symbol): return
    price = get_price(symbol)
    if not price or not set_leverage_for_symbol(symbol, leverage, direction, "BUY" if direction == "LONG" else "SELL"): return
    
    qty = round(trade_size / price, 6)
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction, "type": "MARKET", "quantity": str(qty)
    })
    with dca_lock:
        active_dca[symbol] = {
            "side": direction, "entry_static": price, "entry_dynamic": price,
            "executed": 0, "base_trade_size": trade_size, "tp_percent": tp_percent, "sl_percent": sl_percent
        }
        save_dca_data()
    set_tp_sl(symbol, direction, tp_percent, sl_percent)

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    symbol = f"{str(data.get('currency', '')).upper()}-USDT"
    direction = str(data.get("direction", "")).upper()
    
    if direction in ("LONG", "SHORT"):
        # Hier war der Klammerfehler:
        threading.Thread(
            target=execute_trade, 
            args=(
                symbol, 
                direction, 
                int(data.get("leverage", LEVERAGE)), 
                float(data.get("trade_size", TRADE_SIZE)),
                float(data.get("tp_percent", TP_PERCENT)), 
                float(data.get("sl_percent", SL_PERCENT))
            )
        ).start() # Diese Klammern haben gefehlt
        
    return jsonify({"status": "processing"}), 200


@app.route("/ping")
def ping(): return "pong", 200

if __name__ == "__main__":
    load_dca_data()
    threading.Thread(target=monitor_dca, daemon=True).start()
    threading.Thread(target=break_even_watcher, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
