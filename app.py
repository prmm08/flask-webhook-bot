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
LEVERAGE = 20
TRADE_SIZE = 20
TP_PERCENT = 1
SL_PERCENT = 40

# --- DCA SETTINGS ---
DCA_INTERVAL = 10                # Prüfung alle 10 Sekunden (langsamer ist stabiler)
DCA_COUNT = 7
DCA_DEVIATION_PERCENT = 5        # 5% Abstand zur LETZTEN Order
DCA_VOLUME_MULTIPLIER = 1.5
MIN_ORDER_INTERVAL = 10          
API_ORDER_POLL_INTERVAL = 0.5
API_ORDER_POLL_TIMEOUT = 10

# --- SL Control ---
AUTO_SET_SL = False

def dca_key(symbol, side):
    return f"{symbol}:{side}"

active_dca = {}
dca_lock = threading.Lock()

# --- SIGNATURE ---
def sign_bingx(params):
    items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
    query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

# --- API REQUEST ---
def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)
    timeout = 10

    try:
        params_for_sign = dict(params)
        if method == "POST" and "timestamp" not in params_for_sign:
            params_for_sign["timestamp"] = str(int(time.time() * 1000))
        elif method == "GET" and "timestamp" not in params_for_sign:
             params_for_sign["timestamp"] = str(int(time.time() * 1000))

        sorted_params = sorted((k, str(v)) for k, v in params_for_sign.items())
        query = urllib.parse.urlencode(sorted_params)
        signature = sign_bingx(params_for_sign)
        final_query = f"{query}&signature={signature}"

        if method == "GET":
            response = requests.get(f"{url}?{final_query}", headers=headers, timeout=timeout)
        else:
            response = requests.post(f"{url}?{final_query}", headers=headers, timeout=timeout)

        resp_json = response.json()
        if resp_json.get("code") != 0:
            # Filter API Errors to reduce log noise
            if resp_json.get("code") not in [80001]: 
                print(f"[API ERROR] {endpoint}: {resp_json}")
        return resp_json

    except Exception as e:
        print(f"[NETWORK ERROR] {endpoint}: {e}")
        return None

# --- HELPERS ---
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if r and r.get("data"):
        return float(r["data"]["price"])
    return None

def get_positions():
    r = api_request("GET", "/openApi/swap/v2/user/positions")
    return r.get("data", []) if r and r.get("code") == 0 else []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return bool(r and r.get("data"))

def set_leverage_for_symbol(symbol, leverage, position_side, side=None):
    params = {"symbol": symbol, "leverage": str(leverage), "positionSide": position_side}
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    return r and (r.get("code") == 0 or r.get("code") == 80001)

# --- NEW: CRITICAL FIX FOR STATE LOSS ---
def get_last_fill_price(symbol, position_side):
    """Holt den Preis der letzten ausgeführten Order aus der History, um DCA korrekt zu berechnen."""
    try:
        # Hole die letzten 50 Orders (gefiltert nach executed)
        params = {
            "symbol": symbol,
            "limit": 50,
            "timestamp": str(int(time.time() * 1000))
        }
        r = api_request("GET", "/openApi/swap/v2/trade/allOrders", params)
        orders = r.get("data", {}).get("orders", []) if r else []

        # Filtern: Nur FILLED orders, die unsere Richtung haben (BUY für LONG build, SELL für SHORT build)
        # Wenn wir LONG sind, bauen wir mit BUYS auf. Wenn wir SHORT sind, mit SELLS.
        target_side = "BUY" if position_side == "LONG" else "SELL"
        
        filled_orders = [
            o for o in orders 
            if o["status"] == "FILLED" 
            and o["positionSide"] == position_side 
            and o["side"] == target_side
        ]

        if not filled_orders:
            return None

        # Sortieren nach updateTime absteigend (neueste zuerst)
        filled_orders.sort(key=lambda x: x["updateTime"], reverse=True)
        
        last_price = float(filled_orders[0]["avgFilledPrice"])
        executed_count = len(filled_orders) # Ungefähre Anzahl der DCAs
        
        return last_price, executed_count
    except Exception as e:
        print(f"[HISTORY ERROR] {e}")
        return None, 0

# --- TP/SL LOGIC ---
def reset_tp_sl(symbol, position_side=None, cancel_sl=True):
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
    orders = r.get("data", {}).get("orders", []) if r else []
    for order in orders:
        if position_side and order.get("positionSide") != position_side: continue
        if not cancel_sl and order.get("type") == "STOP_MARKET": continue
        api_request("POST", "/openApi/swap/v2/trade/cancelOrder", 
                    {"orderId": order.get("orderId"), "symbol": symbol})

def set_tp_only(symbol, desired_side=None, tp_percent=TP_PERCENT):
    # Einfache TP Logik, feuert nur Order ab
    pos = None
    for i in range(3):
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
        "type": "TAKE_PROFIT_MARKET", "stopPrice": f"{tp:.6f}", 
        "workingType": "MARK_PRICE", "closePosition": "true"
    })

# --- DCA CORE ---
def calculate_dca_qty(base_trade_size, executed, current_price):
    # Prevent exponent from getting too high if history count is weird
    safe_executed = min(executed, 10) 
    multiplier = DCA_VOLUME_MULTIPLIER ** safe_executed
    val_usdt = base_trade_size * multiplier
    
    # Check Min Notional (ca 5 USDT bei BingX)
    if val_usdt < 6: val_usdt = 6
    
    qty = val_usdt / current_price
    return round(qty, 6)

def poll_order_filled(symbol, order_id, timeout=API_ORDER_POLL_TIMEOUT):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        r = api_request("GET", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id})
        status = r.get("data", {}).get("status", "") if r else ""
        if status in ("FILLED", "filled"): return True
        time.sleep(API_ORDER_POLL_INTERVAL)
    return False

def monitor_dca():
    print("[SYSTEM] DCA Monitor gestartet...")
    while True:
        try:
            positions = get_positions()
            for pos in positions:
                symbol, side = pos["symbol"], pos["positionSide"]
                amt = float(pos["positionAmt"])
                if amt == 0: continue

                current_price = get_price(symbol)
                if not current_price: continue

                key = dca_key(symbol, side)
                
                # --- STATE RECOVERY LOGIC ---
                # Wir holen IMMER den echten letzten Fill Preis aus der API, falls wir ihn nicht im RAM haben
                # oder um sicherzugehen, dass wir synchron sind.
                
                last_ref_price = 0.0
                executed_count = 1

                with dca_lock:
                    if key in active_dca:
                        last_ref_price = active_dca[key]["last_order_price"]
                        executed_count = active_dca[key]["executed"]
                    else:
                        # Recovery Mode: Fetch from API
                        hist_price, hist_count = get_last_fill_price(symbol, side)
                        if hist_price:
                            last_ref_price = hist_price
                            executed_count = max(1, hist_count) # Zumindest 1
                            # Update Memory
                            active_dca[key] = {
                                "symbol": symbol, "side": side,
                                "executed": executed_count,
                                "base_trade_size": TRADE_SIZE, # Annahme, da wir History Size schwer rekonstruieren können
                                "next_allowed_time": 0, "placing": False, 
                                "last_order_price": last_ref_price
                            }
                        else:
                            # Fallback: Avg Price (Nur wenn keine History gefunden)
                            last_ref_price = float(pos["avgPrice"])
                            active_dca[key] = {
                                "symbol": symbol, "side": side, "executed": 1,
                                "base_trade_size": TRADE_SIZE, "next_allowed_time": 0,
                                "placing": False, "last_order_price": last_ref_price
                            }

                d = active_dca[key]

                # --- TRIGGER CALCULATION ---
                if side == "LONG":
                    # Preis muss fallen: Current <= Last * 0.95
                    target_price = last_ref_price * (1 - DCA_DEVIATION_PERCENT / 100)
                    trigger = current_price <= target_price
                    diff_pct = (current_price - last_ref_price) / last_ref_price * 100
                else:
                    # Preis muss steigen: Current >= Last * 1.05
                    target_price = last_ref_price * (1 + DCA_DEVIATION_PERCENT / 100)
                    trigger = current_price >= target_price
                    diff_pct = (current_price - last_ref_price) / last_ref_price * 100

                # DEBUG LOGGING (Damit du siehst was passiert)
                # print(f"[DCA CHECK] {symbol} {side} | LastFill: {last_ref_price:.4f} | Curr: {current_price:.4f} | Target: {target_price:.4f} | Diff: {diff_pct:.2f}%")

                if trigger and executed_count < DCA_COUNT and not d["placing"] and time.monotonic() > d["next_allowed_time"]:
                    print(f"[DCA TRIGGERING] {symbol} {side} Gap erreicht! ({diff_pct:.2f}%)")
                    d["placing"] = True
                    
                    qty = calculate_dca_qty(d["base_trade_size"], executed_count, current_price)
                    
                    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                        "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                        "positionSide": side, "type": "MARKET", "quantity": str(qty)
                    })

                    if resp and resp.get("code") == 0:
                        print(f"[DCA SUCCESS] Order platziert: {symbol}")
                        order_id = resp.get("data", {}).get("orderId")
                        if poll_order_filled(symbol, order_id):
                            with dca_lock:
                                d["executed"] += 1
                                d["last_order_price"] = current_price
                                d["next_allowed_time"] = time.monotonic() + MIN_ORDER_INTERVAL
                            
                            time.sleep(2)
                            set_tp_only(symbol, side)
                    else:
                        print(f"[DCA FAIL] API Code: {resp}")
                    
                    d["placing"] = False

        except Exception as e:
            print("[DCA ERROR LOOP]", e)
        
        time.sleep(DCA_INTERVAL)

def tp_sl_watcher():
    while True:
        try:
            for pos in get_positions():
                symbol, side = pos["symbol"], pos["positionSide"]
                if float(pos["positionAmt"]) == 0: continue
                
                r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                orders = r.get("data", {}).get("orders", []) if r else []
                has_tp = any(o.get("type") == "TAKE_PROFIT_MARKET" and o.get("positionSide") == side for o in orders)
                
                if not has_tp: 
                    set_tp_only(symbol, side)
        except: pass
        time.sleep(30)

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if not symbol_exists(symbol): return
    price = get_price(symbol)
    if not price: return

    set_leverage_for_symbol(symbol, leverage, direction)

    qty = round(trade_size / price, 6)
    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction, "type": "MARKET", "quantity": str(qty)
    })
    
    if resp and resp.get("code") == 0:
        print(f"[EXECUTE] Initial Order Filled: {symbol}")
        with dca_lock:
            active_dca[dca_key(symbol, direction)] = {
                "symbol": symbol, "side": direction, 
                "executed": 1, "base_trade_size": trade_size,
                "next_allowed_time": time.monotonic() + MIN_ORDER_INTERVAL,
                "placing": False, "last_order_price": price
            }
        time.sleep(2)
        set_tp_only(symbol, direction, tp_percent)

# --- FLASK ---
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
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))