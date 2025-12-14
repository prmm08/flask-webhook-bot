# -------- VER 3.13: FULL DUAL-EXCHANGE BOT (BINGX & KUCOIN) - WITH AUTO-CLOSE --------
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

# Tracking
active_monitors = {}

# --- AUTH & HILFSFUNKTIONEN ---

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

# --- BINGX LOGIK ---

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except: return False

def execute_long_bingx(symbol):
    price = get_price_generic("BINGX", symbol)
    if not price: return
    qty = round(20 / price, 6)
    ts = str(int(time.time() * 1000))
    params = {"leverage": "20", "positionSide": "LONG", "quantity": str(qty), "side": "BUY", "symbol": symbol, "timestamp": ts, "type": "MARKET"}
    params["signature"] = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=params, headers={"X-BX-APIKEY": BINGX_API_KEY})
    threading.Thread(target=monitor_generic, args=(symbol, price, price*1.04, price*0.98, "BINGX")).start()

def close_bingx(symbol):
    print(f"[BINGX] Schließe Position für {symbol}")
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions", data=params, headers={"X-BX-APIKEY": BINGX_API_KEY})

# --- KUCOIN LOGIK ---

def is_pos_open_kucoin(symbol):
    try:
        endpoint = f"/api/v1/position?symbol={symbol}"
        r = requests.get(KUCOIN_BASE + endpoint, headers=kucoin_headers("GET", endpoint), timeout=10).json()
        return float(r["data"].get("currentQty", 0)) != 0
    except: return False

def execute_long_kucoin(symbol):
    price = get_price_generic("KUCOIN", symbol)
    if not price: return
    # Mode Settings
    set_ep = "/api/v1/position-settings"
    set_data = json.dumps({"symbol": symbol, "leverage": "20", "marginMode": "ISOLATED"})
    requests.post(KUCOIN_BASE + set_ep, data=set_data, headers=kucoin_headers("POST", set_ep, set_data))
    # Order
    ord_ep = "/api/v1/orders"
    payload = {"clientOid": str(uuid.uuid4()), "side": "buy", "symbol": symbol, "type": "market", "size": "1", "leverage": "20", "positionSide": "long"}
    body = json.dumps(payload)
    requests.post(KUCOIN_BASE + ord_ep, data=body, headers=kucoin_headers("POST", ord_ep, body))
    threading.Thread(target=monitor_generic, args=(symbol, price, price*1.04, price*0.98, "KUCOIN")).start()

def close_kucoin(symbol):
    print(f"[KUCOIN] Schließe Position für {symbol}")
    ord_ep = "/api/v1/orders"
    # Market Sell um Long zu schließen (Hedge Mode benötigt positionSide: "long")
    payload = {"clientOid": str(uuid.uuid4()), "side": "sell", "symbol": symbol, "type": "market", "size": "1", "positionSide": "long"}
    body = json.dumps(payload)
    requests.post(KUCOIN_BASE + ord_ep, data=body, headers=kucoin_headers("POST", ord_ep, body))

# --- GEMEINSAMES MONITORING MIT TRIGGER ---

def monitor_generic(symbol, entry, tp, sl, exchange):
    key = f"{exchange}_{symbol}"
    active_monitors[key] = True
    print(f"[MONITOR] START {key} | Entry: {entry:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")
    
    try:
        be_set = False
        while True:
            curr = get_price_generic(exchange, symbol)
            if not curr:
                time.sleep(1)
                continue

            # Break-Even Logik (+2%)
            if not be_set and curr >= entry * 1.02:
                sl = entry
                be_set = True
                print(f"[BE] {key} Triggered! SL auf Entry gesetzt.")

            # EXIT TRIGGER (TP oder SL/BE erreicht)
            if curr >= tp or curr <= sl:
                reason = "TP" if curr >= tp else "SL/BE"
                print(f"[EXIT] {key} Triggered durch {reason} bei Preis: {curr:.4f}")
                
                if exchange == "BINGX":
                    close_bingx(symbol)
                else:
                    close_kucoin(symbol)
                break
                
            time.sleep(1)
    except Exception as e:
        print(f"[ERROR MONITOR] {key}: {e}")
    finally:
        active_monitors[key] = False
        print(f"[MONITOR] END {key}")

# --- FLASK WEBHOOK ---

@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"error": "no currency"}), 400
    
    print(f"\n--- SIGNAL EMPFANGEN: {currency} ---")
    s_bx = f"{currency}-USDT"
    s_kc = "XBTUSDTM" if currency == "BTC" else f"{currency}USDTM"

    # BingX Check & Execute
    if not is_pos_open_bingx(s_bx) and not active_monitors.get(f"BINGX_{s_bx}"):
        threading.Thread(target=execute_long_bingx, args=(s_bx,)).start()
        print(f"[BINGX] Thread gestartet für {s_bx}")

    # KuCoin Check & Execute
    if not is_pos_open_kucoin(s_kc) and not active_monitors.get(f"KUCOIN_{s_kc}"):
        threading.Thread(target=execute_long_kucoin, args=(s_kc,)).start()
        print(f"[KUCOIN] Thread gestartet für {s_kc}")

    return jsonify({"status": "monitoring_started", "currency": currency}), 200

@app.route("/")
def health(): return "Bot Online", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
