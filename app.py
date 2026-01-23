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
USE_SL = False          
SL_PERCENT = 40        
TP_PERCENT = 1         # 1% Gewinn auf den DURCHSCHNITTSPREIS (Break-Even)

DCA_COUNT = 6          
DCA_DEVIATION_PERCENT = 5
DCA_VOLUME_MULTIPLIER = 2
MIN_ORDER_INTERVAL = 30
TRADE_SIZE = 20
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

# --- TP & SL LOGIK (BREAK-EVEN BASIERT) ---
def set_tp_sl(symbol, side, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    # Position abrufen, um den aktuellen Durchschnittspreis (avgPrice) zu erhalten
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    positions = r_pos.get("data", []) if r_pos else []
    pos = next((p for p in positions if p["positionSide"] == side and float(p["positionAmt"]) != 0), None)
    
    if not pos:
        print(f"[INFO] Keine offene Position für {symbol} {side} gefunden.")
        return

    # DER ENTSCHEIDENDE WERT: avgPrice (Durchschnittspreis aller Orders / Break-Even)
    break_even = float(pos["avgPrice"])
    
    # Take Profit basierend auf Break-Even berechnen
    tp_price = break_even * (1 + tp_percent/100) if side == "LONG" else break_even * (1 - tp_percent/100)

    # 1. Bestehende TP/SL Orders löschen (DELETE statt POST cancel)
    r_orders = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
    orders = r_orders.get("data", {}).get("orders", []) if r_orders else []
    for o in orders:
        if o.get("positionSide") == side and o.get("type") in ["TAKE_PROFIT_MARKET", "STOP_MARKET"]:
            api_request("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": o["orderId"]})

    # 2. TAKE PROFIT setzen
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
        "type": "TAKE_PROFIT_MARKET", "stopPrice": f"{tp_price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
    })
    print(f"[TP UPDATE] {symbol} {side} | Break-Even: {break_even:.4f} | Neuer TP: {tp_price:.4f}")

    # 3. STOP LOSS setzen (Falls aktiviert, ebenfalls basierend auf Break-Even oder Initial?)
    # Meistens wird der SL auch vom Break-Even aus berechnet, um das Risiko konstant zu halten.
    if USE_SL:
        sl_price = break_even * (1 - sl_percent/100) if side == "LONG" else break_even * (1 + sl_percent/100)
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
            "type": "STOP_MARKET", "stopPrice": f"{sl_price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
        })
        print(f"[SL UPDATE] Neuer SL bei {sl_price:.6f}")

# --- DCA MONITOR ---
def monitor_dca():
    print("[SYSTEM] DCA Monitor aktiv...")
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
                            "next_allowed_time": time.monotonic() + 10, "placing": False
                        }
                    
                    d = active_dca[key]
                    if d["placing"] or time.monotonic() < d["next_allowed_time"]: continue

                    # Trigger (5% Abstand zur LETZTEN Order)
                    trigger = False
                    if side == "LONG":
                        trigger = curr_price <= d["last_order_price"] * (1 - DCA_DEVIATION_PERCENT/100)
                    else:
                        trigger = curr_price >= d["last_order_price"] * (1 + DCA_DEVIATION_PERCENT/100)

                    if trigger and d["executed"] < DCA_COUNT:
                        d["placing"] = True
                        print(f"[DCA TRIGGER] {symbol} {side} - Letzter Preis: {d['last_order_price']} -> Aktuell: {curr_price}")
                        qty = (TRADE_SIZE * (DCA_VOLUME_MULTIPLIER ** d["executed"])) / curr_price
                        
                        resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                            "positionSide": side, "type": "MARKET", "quantity": str(round(qty, 6))
                        })
                        
                        if resp and resp.get("code") == 0:
                            d["executed"] += 1
                            d["last_order_price"] = curr_price
                            d["next_allowed_time"] = time.monotonic() + MIN_ORDER_INTERVAL
                            # Kurz warten, damit BingX den avgPrice intern aktualisieren kann
                            time.sleep(3)
                            set_tp_sl(symbol, side)
                        d["placing"] = False
        except Exception as e: print(f"[MONITOR ERROR] {e}")
        time.sleep(10)

# --- EXECUTION ---
def execute_trade(symbol, direction, leverage, trade_size):
    key = dca_key(symbol, direction)
    with dca_lock:
        if key in active_dca: 
            print(f"[SIGNAL] Ignoriert: {symbol} bereits aktiv.")
            return

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
        print(f"[INITIAL] {symbol} {direction} eröffnet.")
        with dca_lock:
            active_dca[key] = {
                "symbol": symbol, "side": direction, "executed": 1,
                "last_order_price": price, "placing": False,
                "next_allowed_time": time.monotonic() + MIN_ORDER_INTERVAL
            }
        time.sleep(3) # Zeit für API Update
        set_tp_sl(symbol, direction)
    else:
        with dca_lock: 
            if key in active_dca: del active_dca[key]

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()
    if currency and direction:
        threading.Thread(target=execute_trade, args=(f"{currency}-USDT", direction, LEVERAGE, TRADE_SIZE)).start()
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    threading.Thread(target=monitor_dca, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))