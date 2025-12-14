# -------- VER 3.14: DUAL-EXCHANGE BOT - KUCOIN HEDGE/ISOLATED FIX --------
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

# API Konfiguration (bleibt identisch)
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"
KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE")
KUCOIN_BASE = "https://api-futures.kucoin.com"

app = Flask(__name__)
active_monitors = {}

# --- Hilfsfunktionen ---
def kucoin_headers(method, endpoint, body=""):
    now = str(int(time.time() * 1000))
    sig_str = f"{now}{method.upper()}{endpoint}{body}"
    sig = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), sig_str.encode(), hashlib.sha256).digest()).decode()
    pass_sig = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest()).decode()
    return {
        "KC-API-KEY": KUCOIN_API_KEY, "KC-API-SIGN": sig, "KC-API-TIMESTAMP": now,
        "KC-API-PASSPHRASE": pass_sig, "KC-API-KEY-VERSION": "2", "Content-Type": "application/json"
    }

def get_price_generic(exchange, symbol):
    try:
        if exchange == "BINGX":
            url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
            r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
            return float(r["data"]["price"])
        elif exchange == "KUCOIN":
            endpoint = f"/api/v1/ticker?symbol={symbol}"
            r = requests.get(KUCOIN_BASE + endpoint, headers=kucoin_headers("GET", endpoint), timeout=10).json()
            return float(r["data"]["price"])
    except: return None

# --- BINGX Logik (unverändert) ---
def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def execute_long_bingx(symbol):
    price = get_price_generic("BINGX", symbol)
    if not price: return
    qty = round(20 / price, 6)
    params = {"leverage": "20", "positionSide": "LONG", "quantity": str(qty), "side": "BUY", "symbol": symbol, "timestamp": str(int(time.time() * 1000)), "type": "MARKET"}
    params["signature"] = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=params, headers={"X-BX-APIKEY": BINGX_API_KEY})
    threading.Thread(target=monitor_generic, args=(symbol, price, price*1.04, price*0.98, "BINGX")).start()

# --- KUCOIN Logik (optimiert) ---
def execute_long_kucoin(symbol):
    print(f"[KUCOIN] Versuche Long-Order für {symbol} (Hedge/Isolated)")
    price = get_price_generic("KUCOIN", symbol)
    if not price: return
    
    # 1. Sicherstellen, dass Margin Mode und Leverage korrekt sind (ISOLATED in Großbuchstaben)
    set_ep = "/api/v1/position-settings"
    set_data = json.dumps({"symbol": symbol, "leverage": "20", "marginMode": "ISOLATED"})
    requests.post(KUCOIN_BASE + set_ep, data=set_data, headers=kucoin_headers("POST", set_ep, set_data), timeout=10)

    # 2. Markt-Order platzieren
    ord_ep = "/api/v1/orders"
    payload = {
        "clientOid": str(uuid.uuid4()), 
        "side": "buy", 
        "symbol": symbol, 
        "type": "market", 
        "size": "1", # Menge an Kontrakten!
        "leverage": "20",
        "positionSide": "long" # Zwingend für Hedge Mode
    }
    body = json.dumps(payload)
    resp = requests.post(KUCOIN_BASE + ord_ep, data=body, headers=kucoin_headers("POST", ord_ep, body), timeout=10).json()
    print(f"[KUCOIN] Order Antwort: {resp}")

    if resp.get("code") == "200000":
        threading.Thread(target=monitor_generic, args=(symbol, price, price*1.04, price*0.98, "KUCOIN")).start()

# --- Monitoring & Close (unverändert) ---
def monitor_generic(symbol, entry, tp, sl, exchange):
    key = f"{exchange}_{symbol}"
    active_monitors[key] = True
    try:
        be_set = False
        while True:
            curr = get_price_generic(exchange, symbol)
            if not curr: time.sleep(1); continue
            if not be_set and curr >= entry * 1.02: sl, be_set = entry, True
            if curr >= tp or curr <= sl:
                if exchange == "BINGX":
                    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
                    params["signature"] = sign_bingx(params)
                    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions", data=params, headers={"X-BX-APIKEY": BINGX_API_KEY})
                else:
                    payload = {"clientOid": str(uuid.uuid4()), "side": "sell", "symbol": symbol, "type": "market", "size": "1", "positionSide": "long"}
                    body = json.dumps(payload)
                    requests.post(KUCOIN_BASE + "/api/v1/orders", data=body, headers=kucoin_headers("POST", "/api/v1/orders", body))
                break
            time.sleep(1)
    finally: active_monitors[key] = False

@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"error": "no currency"}), 400
    s_bx, s_kc = f"{currency}-USDT", ("XBTUSDTM" if currency == "BTC" else f"{currency}USDTM")
    threading.Thread(target=execute_long_bingx, args=(s_bx,)).start()
    threading.Thread(target=execute_long_kucoin, args=(s_kc,)).start()
    return jsonify({"status": "monitoring_started", "currency": currency}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
