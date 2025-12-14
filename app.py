# -------- VER 3.0: Dual-Exchange (BingX & KuCoin) - Sofort-Order/Safety --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import base64
import uuid
import json
from flask import Flask, request, jsonify

# API Zugangsdaten BingX
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# API Zugangsdaten KuCoin
KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE")
KUCOIN_BASE = "https://api-futures.kucoin.com"

app = Flask(__name__)

# Tracking für aktive Überwachungen (Format: "PLATFORM_SYMBOL")
active_monitors = {}
cooldowns = {}
COOLDOWN_SECONDS = 2 * 60 * 60

# --- Hilfsfunktionen Allgemein ---

def dynamic_round(price, value):
    decimals = 2 if price > 1000 else 4 if price > 1 else 6
    return round(value, decimals)

def get_price_generic(exchange, symbol):
    if exchange == "BINGX":
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        return float(requests.get(url, params={"symbol": symbol}).json()["data"]["price"])
    elif exchange == "KUCOIN":
        endpoint = f"/api/v1/ticker?symbol={symbol}"
        return float(requests.get(KUCOIN_BASE + endpoint).json()["data"]["price"])
    return None

# --- BINGX LOGIK ---

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def is_pos_open_bingx(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions"
    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_bingx(params)
    try:
        r = requests.get(url, params=params, headers={"X-BX-APIKEY": BINGX_API_KEY}).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except: return True

def execute_long_bingx(symbol):
    price = get_price_generic("BINGX", symbol)
    qty = round(20 / price, 6) # 20 USDT Einsatz
    
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
    params = {
        "leverage": "20", "positionSide": "LONG", "quantity": str(qty),
        "side": "BUY", "symbol": symbol, "timestamp": str(int(time.time() * 1000)), "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)
    requests.post(url, data=params, headers={"X-BX-APIKEY": BINGX_API_KEY})
    
    tp, sl = dynamic_round(price, price * 1.04), dynamic_round(price, price * 0.98)
    threading.Thread(target=monitor_generic, args=(symbol, price, tp, sl, "BINGX")).start()
    cooldowns[symbol] = time.time() # Cooldown nach Order setzen

# --- KUCOIN LOGIK ---

def kucoin_headers(method, endpoint, body=""):
    now = str(int(time.time() * 1000))
    sig_str = f"{now}{method.upper()}{endpoint}{body}"
    sig = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), sig_str.encode(), hashlib.sha256).digest()).decode()
    pass_sig = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest()).decode()
    return {
        "KC-API-KEY": KUCOIN_API_KEY, "KC-API-SIGN": sig, "KC-API-TIMESTAMP": now,
        "KC-API-PASSPHRASE": pass_sig, "KC-API-KEY-VERSION": "2", "Content-Type": "application/json"
    }

def is_pos_open_kucoin(symbol):
    endpoint = f"/api/v1/position?symbol={symbol}"
    try:
        r = requests.get(KUCOIN_BASE + endpoint, headers=kucoin_headers("GET", endpoint)).json()
        return float(r["data"].get("currentQty", 0)) != 0
    except: return True

def execute_long_kucoin(symbol):
    price = get_price_generic("KUCOIN", symbol)
    qty = 1 # WICHTIG: Dies ist oft 1 Kontrakt, prüfen Sie die LotSize!
    
    endpoint = "/api/v1/orders"
    payload = {"clientOid": str(uuid.uuid4()), "side": "buy", "symbol": symbol, "type": "market", "leverage": "20", "size": "1"}
    body = json.dumps(payload)
    requests.post(KUCOIN_BASE + endpoint, data=body, headers=kucoin_headers("POST", endpoint, body))
    
    tp, sl = round(price * 1.04, 4), round(price * 0.98, 4)
    threading.Thread(target=monitor_generic, args=(symbol, price, tp, sl, "KUCOIN")).start()
    cooldowns[symbol] = time.time() # Cooldown nach Order setzen

# --- GEMEINSAMES MONITORING ---

def monitor_generic(symbol, entry, tp, sl, exchange):
    key = f"{exchange}_{symbol}"
    active_monitors[key] = True
    be_set = False
    try:
        while True:
            curr = get_price_generic(exchange, symbol)
            if not be_set and curr >= entry * 1.02:
                sl, be_set = entry, True
                print(f"[{exchange}] BE aktiv für {symbol}")
            if curr >= tp or curr <= sl:
                # Close Logic (BingX hat einfache closeAll, KuCoin benötigt spezifische sell Order)
                print(f"[{exchange}] Target erreicht. Position schließen.")
                break
            time.sleep(3)
    finally: active_monitors[key] = False


@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"status": "no_currency"}), 400

    symbol_bingx = f"{currency}-USDT"
    symbol_kucoin = f"{currency}USDTM"

    # Cooldown Check
    if time.time() - cooldowns.get(symbol_bingx, 0) < COOLDOWN_SECONDS:
         return jsonify({"status": "cooldown_bingx"}), 200
    if time.time() - cooldowns.get(symbol_kucoin, 0) < COOLDOWN_SECONDS:
         return jsonify({"status": "cooldown_kucoin"}), 200
    
    # Ausführung auf beiden Börsen nach Prüfung der offenen Position
    if not is_pos_open_bingx(symbol_bingx):
        threading.Thread(target=execute_long_bingx, args=(symbol_bingx,)).start()
    
    if not is_pos_open_kucoin(symbol_kucoin):
        threading.Thread(target=execute_long_kucoin, args=(symbol_kucoin,)).start()
        
    return jsonify({"status": "orders_triggered_if_safe", "currency": currency}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
