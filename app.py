import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
from flask import Flask, request, jsonify

# --- CONFIG ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# --- STRATEGIE EINSTELLUNGEN ---
TP_MODE = "FIRST_ORDER"    # OPTIONEN: "AVERAGE" (Break-Even) oder "FIRST_ORDER"
USE_SL = False          
SL_PERCENT = 40       
TP_PERCENT = 1         

DCA_COUNT = 6          
DCA_DEVIATION_PERCENT = 5
DCA_VOLUME_MULTIPLIER = 2
MIN_ORDER_INTERVAL = 30
TRADE_SIZE = 40
LEVERAGE = 20

active_dca = {}
dca_lock = threading.Lock()

def dca_key(symbol, side):
    return f"{symbol}:{side}"

# --- API CORE ---
def sign_bingx(params):
    items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
    query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = dict(params) if params else {}
    params["timestamp"] = str(int(time.time() * 1000))
    
    query_string = urllib.parse.urlencode(sorted(params.items()))
    signature = sign_bingx(params)
    full_url = f"{url}?{query_string}&signature={signature}"

    try:
        if method == "POST":
            response = requests.post(full_url, headers=headers, timeout=10)
        elif method == "GET":
            response = requests.get(full_url, headers=headers, timeout=10)
        elif method == "DELETE":
            response = requests.delete(full_url, headers=headers, timeout=10)
        
        res = response.json()
        if res.get("code") != 0 and res.get("code") != 80001:
            print(f"[API ERROR] {endpoint}: {res}")
        return res
    except Exception as e:
        print(f"[NETWORK ERROR] {e}")
        return None

# --- HELPERS ---
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

def get_last_fill_price(symbol, side):
    try:
        r = api_request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": symbol, "limit": 20})
        orders = r.get("data", {}).get("orders", []) if r else []
        target_side = "BUY" if side == "LONG" else "SELL"
        filled = [o for o in orders if o["status"] == "FILLED" and o["positionSide"] == side and o["side"] == target_side]
        if not filled: return None, 0
        filled.sort(key=lambda x: x["updateTime"], reverse=True)
        return float(filled[0]["avgFilledPrice"]), len(filled)
    except: return None, 0

# --- TP & SL LOGIK MIT MODUS-AUSWAHL ---
def set_tp_sl(symbol, side, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    positions = r_pos.get("data", []) if r_pos else []
    pos = next((p for p in positions if p["positionSide"] == side and float(p["positionAmt"]) != 0), None)
    
    if not pos: return

    # MODUS LOGIK
    if TP_MODE == "AVERAGE":
        base_price = float(pos["avgPrice"]) # Break-Even
    else:
        # Wir suchen den Preis der ALLERERSTEN Order in unserem Speicher
        key = dca_key(symbol, side)
        with dca_lock:
            if key in active_dca and "initial_price" in active_dca[key]:
                base_price = active_dca[key]["initial_price"]
            else:
                base_price = float(pos["avgPrice"]) # Fallback

    tp_price = base_price * (1 + tp_percent/100) if side == "LONG" else base_price * (1 - tp_percent/100)

    # Alte löschen
    r_orders = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
    orders = r_orders.get("data", {}).get("orders", []) if r_orders else []
    for o in orders:
        if o.get("positionSide") == side and o.get("type") in ["TAKE_PROFIT_MARKET", "STOP_MARKET"]:
            api_request("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": o["orderId"]})

    # Neue setzen
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
        "type": "TAKE_PROFIT_MARKET", "stopPrice": f"{tp_price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
    })
    print(f"[TP UPDATE] Modus: {TP_MODE} | Basis: {base_price:.4f} | Ziel: {tp_price:.4f}")

    if USE_SL:
        # SL orientiert sich meist immer am Break-Even um das Gesamtrisiko zu deckeln
        current_avg = float(pos["avgPrice"])
        sl_price = current_avg * (1 - sl_percent/100) if side == "LONG" else current_avg * (1 + sl_percent/100)
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
            "type": "STOP_MARKET", "stopPrice": f"{sl_price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
        })

# --- DCA MONITOR ---
def monitor_dca():
    while True:
        try:
            r_pos = api_request("GET", "/openApi/swap/v2/user/positions")
            positions = r_pos.get("data", []) if r_pos else []
            
            current_keys = [dca_key(p["symbol"], p["positionSide"]) for p in positions if float(p["positionAmt"]) != 0]
            with dca_lock:
                for key in list(active_dca.keys()):
                    if key not in current_keys and not active_dca[key].get("placing"):
                        del active_dca[key]

            for pos in positions:
                symbol, side = pos["symbol"], pos["positionSide"]
                if float(pos["positionAmt"]) == 0: continue
                
                key = dca_key(symbol, side)
                curr_price = get_price(symbol)
                if not curr_price: continue

                with dca_lock:
                    if key not in active_dca:
                        last_p, count = get_last_fill_price(symbol, side)
                        active_dca[key] = {
                            "symbol": symbol, "side": side, "executed": count if count > 0 else 1,
                            "last_order_price": last_p if last_p else float(pos["avgPrice"]),
                            "initial_price": float(pos["avgPrice"]), # Als Referenz speichern
                            "next_allowed_time": time.monotonic() + 10, "placing": False
                        }
                    
                    d = active_dca[key]
                    if d["placing"] or time.monotonic() < d["next_allowed_time"]: continue

                    trigger = False
                    if side == "LONG":
                        trigger = curr_price <= d["last_order_price"] * (1 - DCA_DEVIATION_PERCENT/100)
                    else:
                        trigger = curr_price >= d["last_order_price"] * (1 + DCA_DEVIATION_PERCENT/100)

                    if trigger and d["executed"] < DCA_COUNT:
                        d["placing"] = True
                        qty = (TRADE_SIZE * (DCA_VOLUME_MULTIPLIER ** d["executed"])) / curr_price
                        resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                            "positionSide": side, "type": "MARKET", "quantity": str(round(qty, 6))
                        })
                        if resp and resp.get("code") == 0:
                            d["executed"] += 1
                            d["last_order_price"] = curr_price
                            d["next_allowed_time"] = time.monotonic() + MIN_ORDER_INTERVAL
                            time.sleep(3)
                            set_tp_sl(symbol, side)
                        d["placing"] = False
        except: pass
        time.sleep(10)

# --- EXECUTION ---
def execute_trade(symbol, direction, leverage, trade_size):
    key = dca_key(symbol, direction)
    with dca_lock:
        if key in active_dca: return

    api_request("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol, "leverage": str(leverage), 
        "side": "BUY" if direction == "LONG" else "SELL", "positionSide": direction
    })

    price = get_price(symbol)
    if not price: return

    with dca_lock:
        active_dca[key] = {"placing": True, "next_allowed_time": time.monotonic() + 60}

    qty = round(trade_size / price, 6)
    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction, "type": "MARKET", "quantity": str(qty)
    })

    if resp and resp.get("code") == 0:
        with dca_lock:
            active_dca[key] = {
                "symbol": symbol, "side": direction, "executed": 1,
                "last_order_price": price, 
                "initial_price": price, # Hier speichern wir den Preis der allerersten Order
                "placing": False,
                "next_allowed_time": time.monotonic() + MIN_ORDER_INTERVAL
            }
        time.sleep(3)
        set_tp_sl(symbol, direction)
    else:
        with dca_lock: 
            if key in active_dca: del active_dca[key]

# --- FLASK ---
@app.route("/ping")
@app.route("/")
def health_check(): return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()
    if currency and direction:
        threading.Thread(target=execute_trade, args=(f"{currency}-USDT", direction, LEVERAGE, TRADE_SIZE), daemon=True).start()
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    threading.Thread(target=monitor_dca, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))