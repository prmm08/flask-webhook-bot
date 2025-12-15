# -------- VER 4.3: BINGX FUTURES ONLY - FINAL VERIFIED CODE MIT GET/POST FIX --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# --- API Konfiguration BingX ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# Globaler Status für aktive Überwachungen
active_monitors = {}

# --- HILFSFUNKTIONEN ---

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except: return None

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except: return True

def close_bingx(symbol):
    print(f"[BINGX] Schließe Position für {symbol}")
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions", data=params, headers={"X-BX-APIKEY": API_KEY})

# --- ORDER & MONITORING LOGIK ---

def execute_long_bingx(symbol):
    price = get_price_bingx(symbol)
    if not price: return
    qty = round(20 / price, 6)
    params = {"leverage": "20", "positionSide": "LONG", "quantity": str(qty), "side": "BUY", "symbol": symbol, "timestamp": str(int(time.time() * 1000)), "type": "MARKET"}
    params["signature"] = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=params, headers={"X-BX-APIKEY": API_KEY})
    threading.Thread(target=monitor_position, args=(symbol, price, price*1.04, price*0.98)).start()

def monitor_position(symbol, entry, tp, sl):
    key = f"BINGX_{symbol}"
    active_monitors[key] = True
    try:
        be_set = False
        while True:
            curr = get_price_bingx(symbol)
            if not curr: time.sleep(1); continue
            if not be_set and curr >= entry * 1.02: sl, be_set = entry, True
            if curr >= tp or curr <= sl:
                close_bingx(symbol)
                break
            time.sleep(1)
    except: pass
    finally: active_monitors[key] = False

# --- FLASK WEBHOOK HANDLER ---

@app.route("/testorder", methods=["GET", "POST"])
def handle_alert():
    """
    Endpunkt für Handelssignale UND Verifizierung.
    Akzeptiert GET (Verifizierung) und POST (Signale).
    """
    if request.method == 'GET':
        return jsonify({"status": "ok", "message": "Webhook erreichbar auf testorder"}), 200

    # Wenn Methode POST ist, verarbeite das Signal
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"error": "no currency"}), 400
    
    symbol = f"{currency}-USDT"
    if not is_pos_open_bingx(symbol) and not active_monitors.get(f"BINGX_{symbol}"):
        threading.Thread(target=execute_long_bingx, args=(symbol,)).start()
        return jsonify({"status": "order_started", "symbol": symbol}), 200
    else:
        return jsonify({"status": "already_active", "symbol": symbol}), 200

# ---------------- HEALTH CHECK / VERIFIZIERUNG DER BASIS-URL ----------------

@app.route("/", methods=["GET", "POST"])
def health_check():
    """
    Dieser Endpunkt antwortet auf GET/POST Anfragen zur Verifizierung der Basis-URL.
    """
    return jsonify({"status": "ok", "message": "Webhook erreichbar auf Root-URL"}), 200

# --- APP START ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
