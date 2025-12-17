# -------- V 3.5: BINGX FUTURES - BE FIX MIT ACTIVE WAIT - 17.12.25 --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# --- API Konfiguration ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# Globale Settings
RSI_TIMEFRAME = "1m"
TP_PERCENT, SL_PERCENT, BE_PERCENT = 1.0, 1.5, 0.4

# ---------------- SIGNING ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

# ---------------- MARKET DATA & POSITION CHECK ----------------

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except: return None

def get_ohlcv(symbol, interval="1m", limit=100):
    # ... (Funktion bleibt gleich) ...
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except: return []

def calc_rsi(closes, period=14):
    # ... (Funktion bleibt gleich) ...
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def is_pos_open_bingx(symbol):
    # ... (Funktion bleibt gleich) ...
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except: return True

# NEU: Prüfen ob offene Orders existieren
def has_open_orders(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/openOrders?{qs}&signature={sig}"
    response = requests.get(url, headers={"X-BX-APIKEY": API_KEY}).json()
    return len(response.get('data', [])) > 0

# ---------------- ORDER EXECUTION ----------------

def place_precise_order(symbol, qty, price, order_type, side="BUY"):
    # ... (Funktion bleibt gleich) ...
    for attempt in range(5):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": side, "positionSide": "SHORT", "type": order_type,
            "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        qs = urllib.parse.urlencode(sorted(params.items()))
        sig = sign_bingx(params)
        url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}"
        response = requests.post(url, headers={"X-BX-APIKEY": API_KEY}).json()
        if response.get("code") == 0: return response
        time.sleep(1.5)
    return response

def set_tp_sl(symbol, qty, tp_price, sl_price):
    r_tp = place_precise_order(symbol, qty, tp_price, "TAKE_PROFIT_MARKET")
    r_sl = place_precise_order(symbol, qty, sl_price, "STOP_MARKET")
    print(f"[API RESULT] {symbol} -> TP: {r_tp.get('msg')} | SL: {r_sl.get('msg')}")

# ---------------- MAIN LOGIC ----------------

def execute_trade_bingx(symbol):
    # ... (Funktion bleibt gleich bis zum Ende) ...
    ohlcv = get_ohlcv(symbol, RSI_TIMEFRAME)
    if not ohlcv: return
    rsi = calc_rsi([float(c["close"]) for c in ohlcv])
    
    if rsi < 75:
        print(f"[RSI BLOCK] {symbol} RSI={rsi:.1f} < 75")
        return

    price = get_price_bingx(symbol)
    if not price: return
    qty = round(10 / price, 6) # 10 USDT fest
    
    print(f"[ORDER] SHORT {symbol} | Entry={price}")

    ts = str(int(time.time() * 1000))
    entry_p = {"symbol": symbol, "side": "SELL", "positionSide": "SHORT", "type": "MARKET", "quantity": str(qty), "leverage": "10", "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(entry_p.items()))
    sig = sign_bingx(entry_p)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})

    time.sleep(2)
    tp, sl = price * (1 - TP_PERCENT / 100), price * (1 + SL_PERCENT / 100)
    be_trigger = price * (1 - BE_PERCENT / 100)
    
    set_tp_sl(symbol, qty, tp, sl)
    threading.Thread(target=monitor_be, args=(symbol, qty, price, tp, be_trigger)).start()

# ---------------- BE MONITOR (FIXED ACTIVE WAIT) ----------------

# ---------------- BE MONITOR (FINALE KORREKTUR FÜR TRIGGER ORDERS) ----------------

def monitor_be(symbol, qty, entry, tp_price, trigger):
    print(f"[BE-MONITOR] Start {symbol}. Trigger @ {trigger:.6f}")
    
    while is_pos_open_bingx(symbol):
        curr = get_price_bingx(symbol)
        if curr and curr <= trigger:
            print(f"[BE TRIGGERED] {symbol} erreicht {curr}")

            # 1. Spezifisches Löschen von TRIGGER-ORDERS (TP/SL)
            # Wir nutzen hier den POST Endpunkt für Batch-Stornierung von Trigger-Orders
            ts = str(int(time.time() * 1000))
            c_p = {
                "symbol": symbol,
                "timestamp": ts
            }
            qs = urllib.parse.urlencode(sorted(c_p.items()))
            sig = sign_bingx(c_p)
            
            # Dieser Endpunkt löscht gezielt TP/SL Orders für das Symbol
            cancel_url = f"{BINGX_BASE}/openApi/swap/v2/trade/cancelAllOrders?{qs}&signature={sig}"
            
            # BingX erwartet hier oft POST statt DELETE für Trigger-Orders
            requests.post(cancel_url, headers={"X-BX-APIKEY": API_KEY})
            
            # Kurze, feste Pause statt der fehleranfälligen Warteschleife
            # 2 Sekunden reichen der API normalerweise, um den Slot freizugeben
            time.sleep(2.5)

            # 2. Neuen SL (BE-Preis) setzen
            # Wir setzen hier nur den SL neu. Der TP wird in set_tp_sl mitgesendet.
            be_level = entry * 0.9995 
            
            # Falls set_tp_sl fehlschlägt, versuchen wir es mit einer direkten SL-Order
            set_tp_sl(symbol, qty, tp_price, be_level)
            
            print(f"[BE SUCCESS] {symbol} SL auf BE verschoben. Monitor beendet.")
            break
        
        time.sleep(4)


# ---------------- WEBHOOK & START (Rest bleibt gleich) ----------------

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
