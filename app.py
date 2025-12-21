# -------- V 3.2: BINGX ONLY SHORT REVERSAL (OHNE EMA FILTER) --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify
import logging

# --- API Konfiguration ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# Globale Settings für Reversal-Shorts
TP_PERCENT = 3.0   # Take Profit
SL_PERCENT = 1.5   # Stop Loss
LEVERAGE = 10      # Hebel
TRADE_SIZE = 10    # USDT Einsatz

# Lock gegen Race Conditions
order_lock = threading.Lock()

# ---------------- SIGNING & HELPERS ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except: return None

# ---------------- POSITION ACTIONS ----------------

def has_active_position(symbol):
    """ Prüft, ob bereits eine Position für diesen Coin offen ist """
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        for p in r.get("data", []):
             if abs(float(p.get("positionAmt", 0))) > 0: 
                 return True
        return False
    except: 
        return False

def set_tp_sl(symbol, qty, tp_price, sl_price):
    """ Setzt Take Profit und Stop Loss für Short (Exit-Seite ist BUY) """
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "BUY", "positionSide": "SHORT",
            "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        qs = urllib.parse.urlencode(sorted(params.items()))
        sig = sign_bingx(params)
        url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}"
        requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    
    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- EXECUTION LOGIC ----------------

def execute_reversal_short(symbol):
    with order_lock:
        # 1. Double-Order Check
        if has_active_position(symbol):
            print(f"[SKIP] Short für {symbol} bereits aktiv.")
            return

        # 2. Preis abfragen
        price = get_price_bingx(symbol)
        if not price: return

        # 3. Mengenberechnung
        qty = round(TRADE_SIZE / price, 6)
        
        print(f"[REVERSAL SHORT] Trigger für {symbol} bei Preis {price}")

        # 4. Market Short Order platzieren
        ts = str(int(time.time() * 1000))
        entry_p = {
            "symbol": symbol, 
            "side": "SELL", 
            "positionSide": "SHORT", 
            "type": "MARKET", 
            "quantity": str(qty), 
            "leverage": str(LEVERAGE), 
            "timestamp": ts
        }
        qs = urllib.parse.urlencode(sorted(entry_p.items()))
        sig = sign_bingx(entry_p)
        r = requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}).json()

        if r.get("code") == 0:
            time.sleep(2)
            # 5. TP / SL Kalkulation (Short: TP unten, SL oben)
            tp = price * (1 - TP_PERCENT / 100)
            sl = price * (1 + SL_PERCENT / 100)
            set_tp_sl(symbol, qty, tp, sl)
            print(f"[SUCCESS] Short {symbol} gesetzt. TP: {tp:.4f}, SL: {sl:.4f}")
        else:
            print(f"[ERROR] Order fehlgeschlagen: {r}")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    
    if not currency: 
        return jsonify({"status": "error", "message": "no_currency"}), 400
    
    symbol = f"{currency}-USDT"
    
    # Starte Short-Logik in eigenem Thread
    threading.Thread(target=execute_reversal_short, args=(symbol,)).start()
    
    return jsonify({"status": "short_triggered", "symbol": symbol}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
