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

# --- API Konfiguration ---
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE")
KUCOIN_BASE = "https://api-futures.kucoin.com"

app = Flask(__name__)

active_monitors = {}
cooldowns = {}
COOLDOWN_SECONDS = 2 * 60 * 60

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

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        query = urllib.parse.urlencode(sorted(params.items()))
        sig = hmac.new(BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except: return False

def is_pos_open_kucoin(symbol):
    try:
        endpoint = f"/api/v1/position?symbol={symbol}"
        r = requests.get(KUCOIN_BASE + endpoint, headers=kucoin_headers("GET", endpoint), timeout=10).json()
        return float(r["data"].get("currentQty", 0)) != 0
    except: return False

def execute_long_bingx(symbol):
    print(f"[BINGX] Starte Order für {symbol}")
    price = get_price_generic("BINGX", symbol)
    if not price: return
    qty = round(20 / price, 6)
    ts = str(int(time.time() * 1000))
    params = {"leverage": "20", "positionSide": "LONG", "quantity": str(qty), "side": "BUY", "symbol": symbol, "timestamp": ts, "type": "MARKET"}
    query = urllib.parse.urlencode(sorted(params.items()))
    params["signature"] = hmac.new(BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    resp = requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=params, headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=10).json()
    print(f"[BINGX] Response: {resp}")
    threading.Thread(target=monitor_generic, args=(symbol, price, price*1.04, price*0.98, "BINGX")).start()

def execute_long_kucoin(symbol):
    print(f"[KUCOIN] Starte Order für {symbol}")
    price = get_price_generic("KUCOIN", symbol)
    if not price: return
    
    # 1. Margin Mode Isolated sicherstellen
    set_ep = "/api/v1/position-settings"
    set_data = json.dumps({"symbol": symbol, "marginMode": "ISOLATED", "leverage": "20"})
    requests.post(KUCOIN_BASE + set_ep, data=set_data, headers=kucoin_headers("POST", set_ep, set_data), timeout=10)

    # 2. Order senden
    ord_ep = "/api/v1/orders"
    payload = {"clientOid": str(uuid.uuid4()), "side": "buy", "symbol": symbol, "type": "market", "size": "1", "leverage": "20"}
    body = json.dumps(payload)
    resp = requests.post(KUCOIN_BASE + ord_ep, data=body, headers=kucoin_headers("POST", ord_ep, body), timeout=10).json()
    print(f"[KUCOIN] Response: {resp}")
    threading.Thread(target=monitor_generic, args=(symbol, price, price*1.04, price*0.98, "KUCOIN")).start()

def monitor_generic(symbol, entry, tp, sl, exchange):
    key = f"{exchange}_{symbol}"
    active_monitors[key] = True
    print(f"[MONITOR START] {key} | Entry: {entry}")
    try:
        be_set = False
        while True:
            curr = get_price_generic(exchange, symbol)
            if not curr: time.sleep(1); continue
            if not be_set and curr >= entry * 1.02:
                sl, be_set = entry, True
                print(f"[BE] {key} aktiviert")
            if curr >= tp or curr <= sl:
                print(f"[EXIT] {key} bei {curr}")
                break
            time.sleep(1)
    finally: active_monitors[key] = False

@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"error": "no currency"}), 400

    print(f"\n--- SIGNAL: {currency} ---")
    
    s_bx = f"{currency}-USDT"
    s_kc = "XBTUSDTM" if currency == "BTC" else f"{currency}USDTM"

    # BingX Check & Start
    if not is_pos_open_bingx(s_bx) and not active_monitors.get(f"BINGX_{s_bx}"):
        threading.Thread(target=execute_long_bingx, args=(s_bx,)).start()
    
    # KuCoin Check & Start
    if not is_pos_open_kucoin(s_kc) and not active_monitors.get(f"KUCOIN_{s_kc}"):
        threading.Thread(target=execute_long_kucoin, args=(s_kc,)).start()

    return jsonify({"status": "monitoring_started", "currency": currency}), 200

@app.route("/")
def health(): return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
