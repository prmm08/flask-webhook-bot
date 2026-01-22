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

# --- API ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- DEFAULT SETTINGS ---
LEVERAGE = 20
TRADE_SIZE = 20
TP_PERCENT = 1
SL_PERCENT = 40

# --- DCA SETTINGS ---
DCA_INTERVAL = 5
DCA_COUNT = 7
DCA_DEVIATION_PERCENT = 5        # 5% Abstand zwischen den Orders
DCA_VOLUME_MULTIPLIER = 1.5
MIN_ORDER_INTERVAL = 10          # Sekunden
HYSTERESIS = 0.002               
API_ORDER_POLL_INTERVAL = 0.5
API_ORDER_POLL_TIMEOUT = 10

# --- SL Steuerung ---
AUTO_SET_SL = False

def dca_key(symbol, side):
    return f"{symbol}:{side}"

active_dca = {}
dca_lock = threading.Lock()
last_dca_heartbeat = time.monotonic()

# --- SIGNATURE ---
def sign_bingx(params):
    if not params:
        query_string = ""
    else:
        items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
        query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

# --- API REQUEST ---
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

# --- HELPERS ---
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
    return bool(r and "data" in r)

def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(leverage), "timestamp": ts}
    if position_side: params["positionSide"] = position_side
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    return bool(r)

def reset_tp_sl(symbol, position_side=None, cancel_sl=True):
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
    orders = r.get("data", {}).get("orders", []) if r else []
    for order in orders:
        if position_side and order.get("positionSide") != position_side: continue
        if not cancel_sl and order.get("type") == "STOP_MARKET": continue
        api_request("POST", "/openApi/swap/v2/trade/cancelOrder", 
                    {"orderId": order.get("orderId"), "symbol": symbol, "timestamp": str(int(time.time() * 1000))})

def set_tp_sl(symbol, desired_side=None, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    pos = None
    for _ in range(5):
        positions = get_positions()
        pos = next((p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0 and (desired_side is None or p["positionSide"] == desired_side)), None)
        if pos: break
        time.sleep(1)
    if not pos: return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])
    tp = entry * (1 + tp_percent/100) if side == "LONG" else entry * (1 - tp_percent/100)
    sl = entry * (1 - sl_percent/100) if side == "LONG" else entry * (1 + sl_percent/100)

    reset_tp_sl(symbol, side, cancel_sl=True)
    for price, otype in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
            "type": otype, "stopPrice": f"{price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
        })

def set_tp_only(symbol, desired_side=None, tp_percent=TP_PERCENT):
    pos = None
    for _ in range(5):
        positions = get_positions()
        pos = next((p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0 and (desired_side is None or p["positionSide"] == desired_side)), None)
        if pos: break
        time.sleep(1)
    if not pos: return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])
    tp = entry * (1 + tp_percent/100) if side == "LONG" else entry * (1 - tp_percent/100)

    reset_tp_sl(symbol, side, cancel_sl=False)
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
        "type": "TAKE_PROFIT_MARKET", "stopPrice": f"{tp:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
    })

# --- DCA CORE ---

def update_entry(symbol, side):
    positions = get_positions()
    pos = next((p for p in positions if p["symbol"] == symbol and p["positionSide"] == side), None)
    return float(pos["avgPrice"]) if pos else None

def calculate_dca_qty(base_trade_size, executed, current_price):
    multiplier = DCA_VOLUME_MULTIPLIER ** (executed)
    qty = (base_trade_size * multiplier) / current_price
    return round(qty, 6)

def poll_order_filled(symbol, order_id, timeout=API_ORDER_POLL_TIMEOUT):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        r = api_request("GET", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id, "timestamp": str(int(time.time() * 1000))})
        if r and r.get("data", {}).get("status") in ("FILLED", "filled"): return True
        time.sleep(API_ORDER_POLL_INTERVAL)
    return False

def monitor_dca():
    global last_dca_heartbeat
    while True:
        last_dca_heartbeat = time.monotonic()
        try:
            positions = get_positions()
            for pos in positions:
                symbol, side = pos["symbol"], pos["positionSide"]
                amt = float(pos["positionAmt"])
                if amt == 0: continue

                current_price = get_price(symbol)
                if not current_price: continue

                key = dca_key(symbol, side)
                with dca_lock:
                    if key not in active_dca:
                        active_dca[key] = {
                            "symbol": symbol, "side": side, "entry_initial": float(pos["avgPrice"]),
                            "executed": 1, "base_trade_size": abs(amt) * float(pos["avgPrice"]),
                            "next_allowed_time": 0, "placing": False, "last_order_price": float(pos["avgPrice"])
                        }
                    d = active_dca[key]

                # --- FIX: TRIGER LOGIK BASIEREND AUF DER LETZTEN ORDER ---
                last_ref_price = d["last_order_price"]
                if side == "LONG":
                    trigger = current_price <= last_ref_price * (1 - DCA_DEVIATION_PERCENT / 100)
                else:
                    trigger = current_price >= last_ref_price * (1 + DCA_DEVIATION_PERCENT / 100)

                if trigger and d["executed"] < DCA_COUNT and not d["placing"] and time.monotonic() > d["next_allowed_time"]:
                    d["placing"] = True
                    qty = calculate_dca_qty(d["base_trade_size"], d["executed"], current_price)
                     
                    print(f"[DCA TRIGGER] {symbol} {side} - Letzter Preis: {last_ref_price} -> Aktuell: {current_price}")
                     
                    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                        "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                        "positionSide": side, "type": "MARKET", "quantity": str(qty)
                    })

                    if resp and (resp.get("data", {}).get("orderId") or resp.get("code") == 0):
                        order_id = resp.get("data", {}).get("orderId")
                        if not order_id or poll_order_filled(symbol, order_id):
                            with dca_lock:
                                d["executed"] += 1
                                d["last_order_price"] = current_price # Setzt neuen Ankerpunkt
                                d["next_allowed_time"] = time.monotonic() + MIN_ORDER_INTERVAL
                             
                            time.sleep(2)
                            if AUTO_SET_SL: set_tp_sl(symbol, side)
                            else: set_tp_only(symbol, side)
                     
                    d["placing"] = False

        except Exception as e:
            print("[DCA ERROR]", e)
        time.sleep(DCA_INTERVAL)

# --- RESTLICHE FUNKTIONEN (Flask/Threads) ---

def tp_sl_watcher():
    while True:
        try:
            for pos in get_positions():
                symbol, side = pos["symbol"], pos["positionSide"]
                if float(pos["positionAmt"]) == 0: continue
                r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": str(int(time.time() * 1000))})
                orders = r.get("data", {}).get("orders", []) if r else []
                has_tp = any(o.get("type") == "TAKE_PROFIT_MARKET" and o.get("positionSide") == side for o in orders)
                if not has_tp: set_tp_only(symbol, side)
        except: pass
        time.sleep(20)

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if not symbol_exists(symbol): return
    price = get_price(symbol)
    if not price or not set_leverage_for_symbol(symbol, leverage, direction): return

    qty = round(trade_size / price, 6)
    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction, "type": "MARKET", "quantity": str(qty)
    })
    
    if resp:
        with dca_lock:
            active_dca[dca_key(symbol, direction)] = {
                "symbol": symbol, "side": direction, "entry_initial": price,
                "executed": 1, "base_trade_size": trade_size,
                "next_allowed_time": time.monotonic() + MIN_ORDER_INTERVAL,
                "placing": False, "last_order_price": price
            }
        time.sleep(2)
        set_tp_only(symbol, direction, tp_percent)

# --- NEUER HEALTH CHECK ENDPOINT (WICHTIG FÜR RENDER) ---
@app.route("/ping")
@app.route("/")
def health_check():
    return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()
    if not currency or direction not in ("LONG", "SHORT"): return jsonify({"status": "ignored"}), 200
    
    threading.Thread(target=execute_trade, args=(f"{currency}-USDT", direction, 
                      int(data.get("leverage", LEVERAGE)), float(data.get("trade_size", TRADE_SIZE)), 
                      float(data.get("tp_percent", TP_PERCENT)), float(data.get("sl_percent", SL_PERCENT)))).start()
    return jsonify({"status": "processing"}), 200

if __name__ == "__main__":
    if API_KEY and API_SECRET:
        threading.Thread(target=monitor_dca, daemon=True).start()
        threading.Thread(target=tp_sl_watcher, daemon=True).start()
        # Auf Render wird der Port über die Environment Variable gesetzt
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))