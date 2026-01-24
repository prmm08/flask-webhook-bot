import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import sys
from flask import Flask, request, jsonify

# --- LOGGING HELPER ---
def log_print(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    sys.stdout.flush()

# --- CONFIG ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# --- STRATEGIE EINSTELLUNGEN ---
TP_MODE = "FIRST_ORDER"        # "AVERAGE" oder "FIRST_ORDER"
USE_SL = False          
SL_PERCENT = 40    
TP_PERCENT = 1             # Dein Standard-Profit Ziel
BE_DCA_LEVEL = 3           # Ab welchem DCA Level soll auf Break-Even umgestellt werden?
BE_PROFIT_PERCENT = 0.05   # Minimaler Profit bei Break-Even (deckt Gebühren)

DCA_COUNT = 4          
DCA_DEVIATION_PERCENT = 5
DCA_VOLUME_MULTIPLIER = 2
MIN_ORDER_INTERVAL = 30
TRADE_SIZE = 200
LEVERAGE = 20

active_dca = {}
dca_lock = threading.Lock()

def dca_key(symbol, side):
    return f"{symbol}:{side}"

# --- TELEGRAM FUNKTION ---
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        log_print(f"[TELEGRAM] Nachricht gesendet.")
    except Exception as e:
        log_print(f"[TELEGRAM ERROR] {e}")

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
        if method == "POST": resp = requests.post(full_url, headers=headers, timeout=15)
        elif method == "GET": resp = requests.get(full_url, headers=headers, timeout=15)
        elif method == "DELETE": resp = requests.delete(full_url, headers=headers, timeout=15)
        res = resp.json()
        if res.get("code") != 0 and res.get("code") != 80001:
            log_print(f"API ERROR {endpoint} | {res}")
        return res
    except Exception as e:
        log_print(f"NETWORK ERROR {endpoint}: {e}")
        return None

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

# --- TP & SL LOGIK ---
def set_tp_sl(symbol, side):
    log_print(f"Setup TP/SL für {symbol} {side}...")
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    positions = r_pos.get("data", []) if r_pos else []
    pos = next((p for p in positions if p["positionSide"] == side and float(p["positionAmt"]) != 0), None)
    
    if not pos:
        return

    # DCA Level prüfen
    key = dca_key(symbol, side)
    current_executed = 1
    with dca_lock:
        if key in active_dca:
            current_executed = active_dca[key].get("executed", 1)

    # Dynamischer TP Target
    target_tp = TP_PERCENT
    if current_executed >= BE_DCA_LEVEL:
        target_tp = BE_PROFIT_PERCENT
        log_print(f"!!! Sicherheitsmodus !!! DCA Level {current_executed} erreicht. TP auf {target_tp}% gesetzt.")

    # Basispreis (Ab Break-Even Level immer Durchschnittspreis nehmen)
    if TP_MODE == "AVERAGE" or current_executed >= BE_DCA_LEVEL:
        base_price = float(pos["avgPrice"])
    else:
        with dca_lock:
            base_price = active_dca.get(key, {}).get("initial_price", float(pos["avgPrice"]))

    tp_price = base_price * (1 + target_tp/100) if side == "LONG" else base_price * (1 - target_tp/100)

    # Alte Orders löschen
    r_orders = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
    open_orders = r_orders.get("data", {}).get("orders", []) if r_orders else []
    for o in open_orders:
        if o.get("positionSide") == side and o.get("type") in ["TAKE_PROFIT_MARKET", "STOP_MARKET"]:
            api_request("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": o["orderId"]})

    # Neue Orders setzen
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
        "type": "TAKE_PROFIT_MARKET", "stopPrice": f"{tp_price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
    })

    if USE_SL:
        avg_p = float(pos["avgPrice"])
        sl_price = avg_p * (1 - SL_PERCENT/100) if side == "LONG" else avg_p * (1 + SL_PERCENT/100)
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
            "type": "STOP_MARKET", "stopPrice": f"{sl_price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
        })
    log_print(f"TP/SL aktualisiert für {symbol}. Ziel: {tp_price:.4f} ({target_tp}%)")

# --- DCA MONITOR ---
def monitor_dca():
    log_print("DCA Monitor gestartet.")
    while True:
        try:
            r_pos = api_request("GET", "/openApi/swap/v2/user/positions")
            positions = r_pos.get("data", []) if r_pos else []
            
            current_keys = [dca_key(p["symbol"], p["positionSide"]) for p in positions if float(p["positionAmt"]) != 0]
            with dca_lock:
                for key in list(active_dca.keys()):
                    if key not in current_keys and not active_dca[key].get("placing"):
                        log_print(f"Trade {key} beendet.")
                        del active_dca[key]

            for pos in positions:
                symbol, side = pos["symbol"], pos["positionSide"]
                if float(pos["positionAmt"]) == 0: continue
                key = dca_key(symbol, side)
                curr_price = get_price(symbol)
                if not curr_price: continue

                with dca_lock:
                    if key not in active_dca:
                        active_dca[key] = {"symbol": symbol, "side": side, "executed": 1, "last_order_price": float(pos["avgPrice"]), "initial_price": float(pos["avgPrice"]), "next_allowed_time": time.monotonic() + 5, "placing": False}
                    
                    d = active_dca[key]
                    if d["placing"] or time.monotonic() < d["next_allowed_time"]: continue

                    diff = ((curr_price / d["last_order_price"]) - 1) * 100
                    trigger = (side == "LONG" and diff <= -DCA_DEVIATION_PERCENT) or (side == "SHORT" and diff >= DCA_DEVIATION_PERCENT)

                    if trigger and d["executed"] < DCA_COUNT:
                        d["placing"] = True
                        log_print(f"DCA TRIGGER {symbol} | Diff: {diff:.2f}%")
                        qty = (TRADE_SIZE * (DCA_VOLUME_MULTIPLIER ** d["executed"])) / curr_price
                        
                        resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                            "positionSide": side, "type": "MARKET", "quantity": str(round(qty, 6))
                        })
                        if resp and resp.get("code") == 0:
                            d["executed"] += 1
                            d["last_order_price"] = curr_price
                            d["next_allowed_time"] = time.monotonic() + MIN_ORDER_INTERVAL
                            
                            if d["executed"] == BE_DCA_LEVEL:
                                send_telegram(f"⚠️ *DCA Level {BE_DCA_LEVEL}*\nSymbol: {symbol}\nModus: Break-Even aktiv!")
                            
                            time.sleep(3)
                            set_tp_sl(symbol, side)
                        d["placing"] = False
        except Exception as e: log_print(f"Monitor Error: {e}")
        time.sleep(20)

# --- EXECUTION ---
def execute_trade(symbol, direction, leverage, trade_size):
    log_print(f"Neues Signal: {symbol} {direction}")
    key = dca_key(symbol, direction)
    with dca_lock:
        if key in active_dca: return

    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": str(leverage), "side": direction, "positionSide": direction})
    price = get_price(symbol)
    if not price: return

    with dca_lock:
        active_dca[key] = {"placing": True, "next_allowed_time": time.monotonic() + 60, "initial_price": price}

    qty = round(trade_size / price, 6)
    resp = api_request("POST", "/openApi/swap/v2/trade/order", {"symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL", "positionSide": direction, "type": "MARKET", "quantity": str(qty)})

    if resp and resp.get("code") == 0:
        log_print(f"Initialkauf erfolgreich.")
        with dca_lock:
            active_dca[key].update({"executed": 1, "last_order_price": price, "placing": False, "next_allowed_time": time.monotonic() + MIN_ORDER_INTERVAL})
        time.sleep(3)
        set_tp_sl(symbol, direction)
    else:
        with dca_lock: 
            if key in active_dca: del active_dca[key]

# --- FLASK ---
@app.route("/ping")
def ping(): return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    log_print(f"Webhook empfangen: {data}")
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()
    if currency and direction in ["LONG", "SHORT"]:
        threading.Thread(target=execute_trade, args=(f"{currency}-USDT", direction, LEVERAGE, TRADE_SIZE), daemon=True).start()
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    threading.Thread(target=monitor_dca, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))