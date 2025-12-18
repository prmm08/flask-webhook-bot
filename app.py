# -------- V 3.7: BINGX FUTURES - BE VIA MARKET CLOSE (LOCAL MONITOR) --------

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

# Globale Settings
RSI_TIMEFRAME = "1m"
TP_PERCENT, SL_PERCENT, BE_PERCENT = 3.0, 1, 0.5

# ---------------- SIGNING ----------------
def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

# ---------------- MARKET DATA ----------------
def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except: return None

# ---------------- POSITION ACTIONS ----------------

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except: return True

def close_position_market(symbol):
    """ Schließt die gesamte Position sofort per Market Order """
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions?{qs}&signature={sig}"
    res = requests.post(url, headers={"X-BX-APIKEY": API_KEY}).json()
    print(f"[EXIT] {symbol} Markt-Close ausgeführt: {res.get('msg')}")

def set_tp_sl(symbol, qty, tp_price, sl_price):
    """ Setzt initiale TP/SL zur Sicherheit """
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

# ---------------- MONITORING ----------------

def monitor_trade(symbol, entry, tp, sl, be_trigger):
    print(f"[MONITOR] Start {symbol}. BE-Trigger: {be_trigger:.6f} | TP: {tp:.6f} | SL: {sl:.6f}")
    
    be_active = False

    while is_pos_open_bingx(symbol):
        curr = get_price_bingx(symbol)
        if not curr:
            time.sleep(2)
            continue

        # 1. Check auf BE Trigger (Profit erreicht)
        if not be_active and curr <= be_trigger:
            be_active = True
            print(f"[BE STATUS] {symbol} Profit erreicht. BE-Schutz ist jetzt scharf geschaltet (Exit bei Entry).")

        # 2. Wenn BE scharf ist: Schließen sobald Kurs wieder auf Entry steigt
        if be_active and curr >= entry:
            print(f"[BE EXIT] {symbol} Kurs zurück am Entry. Schließe Position.")
            close_position_market(symbol)
            break

        # 3. Lokaler Sicherheits-Check auf TP oder SL (falls API Trigger versagen)
        if curr <= tp or curr >= sl:
            reason = "TP" if curr <= tp else "SL"
            print(f"[LOCAL EXIT] {symbol} {reason} erreicht. Schließe...")
            close_position_market(symbol)
            break

        time.sleep(3)
    print(f"[MONITOR] Ende für {symbol}")

# ---------------- EXECUTION ----------------

def execute_trade_bingx(symbol):
    price = get_price_bingx(symbol)
    if not price: return
    
    qty = round(10 / price, 6)
    print(f"[ENTRY] SHORT {symbol} @ {price}")

    # Entry Order
    ts = str(int(time.time() * 1000))
    entry_p = {"symbol": symbol, "side": "SELL", "positionSide": "SHORT", "type": "MARKET", "quantity": str(qty), "leverage": "10", "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(entry_p.items()))
    sig = sign_bingx(entry_p)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})

    time.sleep(2)
    
    # Kalkulationen
    tp = price * (1 - TP_PERCENT / 100)
    sl = price * (1 + SL_PERCENT / 100)
    be_trigger = price * (1 - BE_PERCENT / 100)
    
    # Initiale TP/SL setzen (als Backup)
    set_tp_sl(symbol, qty, tp, sl)
    
    # Lokaler Monitor übernimmt BE Management
    threading.Thread(target=monitor_trade, args=(symbol, price, tp, sl, be_trigger)).start()

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET": return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"status": "ignored"}), 200
    symbol = f"{currency}-USDT"
    
    if not is_pos_open_bingx(symbol):
        threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()
        return jsonify({"status": "started", "symbol": symbol}), 200
    return jsonify({"status": "active"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
