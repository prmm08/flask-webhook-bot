import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
from flask import Flask, request, jsonify
import logging

# --- API CONFIG ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- SETTINGS ---
LEVERAGE = 10
TRADE_SIZE = 10
TP_PERCENT = 1
SL_PERCENT = 40

# --- DCA SETTINGS ---
DCA_INTERVAL = 10                
DCA_COUNT = 7
DCA_DEVIATION_PERCENT = 5        
DCA_VOLUME_MULTIPLIER = 1.5
MIN_ORDER_INTERVAL = 30          
API_ORDER_POLL_INTERVAL = 0.5
API_ORDER_POLL_TIMEOUT = 10

active_dca = {}
dca_lock = threading.Lock()

# --- MISSING FUNCTION (FIXED) ---
def dca_key(symbol, side):
    return f"{symbol}:{side}"

# --- SIGNATURE & API ---
def sign_bingx(params):
    items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
    query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)
    try:
        params_for_sign = dict(params)
        ts = str(int(time.time() * 1000))
        params_for_sign["timestamp"] = ts
        
        sorted_params = sorted((k, str(v)) for k, v in params_for_sign.items())
        query = urllib.parse.urlencode(sorted_params)
        signature = sign_bingx(params_for_sign)
        final_url = f"{url}?{query}&signature={signature}"

        if method == "GET":
            response = requests.get(final_url, headers=headers, timeout=10)
        else:
            response = requests.post(final_url, headers=headers, timeout=10)

        res = response.json()
        if res.get("code") != 0 and res.get("code") != 80001:
            print(f"[API ERROR] {endpoint}: {res}")
        return res
    except Exception as e:
        print(f"[ERROR] API Request failed: {e}")
        return None

# --- HELPERS ---
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        return float(r["data"]["price"])
    except (KeyError, TypeError, ValueError):
        return None

def get_positions():
    r = api_request("GET", "/openApi/swap/v2/user/positions")
    return r.get("data", []) if r and r.get("code") == 0 else []

def get_last_fill_price(symbol, position_side):
    try:
        r = api_request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": symbol, "limit": 20})
        orders = r.get("data", {}).get("orders", []) if r else []
        
        target_side = "BUY" if position_side == "LONG" else "SELL"
        filled = [o for o in orders if o["status"] == "FILLED" and o["positionSide"] == position_side and o["side"] == target_side]
        
        if not filled: return None, 0
        filled.sort(key=lambda x: x["updateTime"], reverse=True)
        return float(filled[0]["avgFilledPrice"]), len(filled)
    except:
        return None, 0

# --- TRADE LOGIC ---
def set_tp_only(symbol, desired_side, tp_percent=TP_PERCENT):
    positions = get_positions()
    pos = next((p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0 and p["positionSide"] == desired_side), None)
    if not pos: return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])
    tp = entry * (1 + tp_percent/100) if side == "LONG" else entry * (1 - tp_percent/100)

    # Bestehende TPs löschen
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
    orders = r.get("data", {}).get("orders", []) if r else []
    for o in orders:
        if o.get("type") in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"] and o.get("positionSide") == side:
            api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"orderId": o["orderId"], "symbol": symbol})

    # Neuen TP setzen
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
        "type": "TAKE_PROFIT_MARKET", "stopPrice": f"{tp:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
    })

def monitor_dca():
    print("[SYSTEM] DCA Monitor aktiv.")
    while True:
        try:
            positions = get_positions()
            for pos in positions:
                symbol, side = pos["symbol"], pos["positionSide"]
                if float(pos["positionAmt"]) == 0: continue

                key = dca_key(symbol, side)
                current_price = get_price(symbol)
                if not current_price: continue

                with dca_lock:
                    if key not in active_dca:
                        hist_price, hist_count = get_last_fill_price(symbol, side)
                        active_dca[key] = {
                            "symbol": symbol, "side": side,
                            "executed": hist_count if hist_count > 0 else 1,
                            "last_order_price": hist_price if hist_price else float(pos["avgPrice"]),
                            "next_allowed_time": time.monotonic() + 15, 
                            "placing": False
                        }
                    
                    d = active_dca[key]
                    if d.get("placing") or time.monotonic() < d.get("next_allowed_time", 0):
                        continue

                    last_price = d["last_order_price"]
                    if side == "LONG":
                        target = last_price * (1 - DCA_DEVIATION_PERCENT / 100)
                        trigger = current_price <= target
                    else:
                        target = last_price * (1 + DCA_DEVIATION_PERCENT / 100)
                        trigger = current_price >= target

                    if trigger and d["executed"] < DCA_COUNT:
                        d["placing"] = True
                        print(f"[DCA] Trigger {symbol} {side} - Aktuell: {current_price} | Ziel: {target}")
                        
                        multiplier = DCA_VOLUME_MULTIPLIER ** d["executed"]
                        qty = (TRADE_SIZE * multiplier) / current_price
                        
                        resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                            "positionSide": side, "type": "MARKET", "quantity": str(round(qty, 6))
                        })

                        if resp and resp.get("code") == 0:
                            d["executed"] += 1
                            d["last_order_price"] = current_price
                            d["next_allowed_time"] = time.monotonic() + MIN_ORDER_INTERVAL
                            time.sleep(2)
                            set_tp_only(symbol, side)
                        
                        d["placing"] = False
        except Exception as e:
            print(f"[DCA ERROR] {e}")
        time.sleep(DCA_INTERVAL)

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    key = dca_key(symbol, direction)
    with dca_lock:
        active_dca[key] = {"placing": True, "next_allowed_time": time.monotonic() + 60} 

    price = get_price(symbol)
    if not price:
        print(f"[EXECUTE FAIL] Preis für {symbol} nicht abrufbar.")
        with dca_lock: 
            if key in active_dca: del active_dca[key]
        return

    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": str(leverage), "positionSide": direction})

    qty = round(trade_size / price, 6)
    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction, "type": "MARKET", "quantity": str(qty)
    })
    
    if resp and resp.get("code") == 0:
        print(f"[EXECUTE SUCCESS] {symbol} {direction}")
        with dca_lock:
            active_dca[key] = {
                "symbol": symbol, "side": direction, "executed": 1,
                "last_order_price": price, "placing": False,
                "next_allowed_time": time.monotonic() + MIN_ORDER_INTERVAL
            }
        time.sleep(3)
        set_tp_only(symbol, direction, tp_percent)
    else:
        with dca_lock:
            if key in active_dca: del active_dca[key]

# --- FLASK ---
@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()
    if not currency or direction not in ("LONG", "SHORT"): return jsonify({"status": "ignored"}), 200
    
    threading.Thread(target=execute_trade, args=(f"{currency}-USDT", direction, 
                      int(data.get("leverage", LEVERAGE)), float(data.get("trade_size", TRADE_SIZE)), 
                      float(data.get("tp_percent", TP_PERCENT)), float(data.get("sl_percent", SL_PERCENT))), daemon=True).start()
    return jsonify({"status": "processing"}), 200

@app.route("/ping")
@app.route("/")
def health(): return "OK", 200

if __name__ == "__main__":
    if API_KEY and API_SECRET:
        threading.Thread(target=monitor_dca, daemon=True).start()
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))