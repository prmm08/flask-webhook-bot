# -------- V 3.2: BINGX FUTURES - KORRIGIERTE SIGNATUR & TIMING --------

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
TP_PERCENT, SL_PERCENT, BE_PERCENT = 0.9, 0.8, 0.4

# ---------------- SIGNING (FIXED) ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

# ---------------- PRICE & OHLCV ----------------

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except:
        return None

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except:
        return []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------- POSITION CHECK ----------------

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except:
        return True

# ---------------- PRECISE TP/SL SETTING (KORRIGIERT) ----------------

def set_tp_sl(symbol, qty, tp_price, sl_price):
    # Preise auf 6 Nachkommastellen runden
    tp_p = "{:.6f}".format(tp_price)
    sl_p = "{:.6f}".format(sl_price)
    
    def place_order(price, order_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol,
            "side": "BUY",  # Exit für SHORT
            "positionSide": "SHORT",
            "type": order_type,
            "quantity": str(qty),
            "stopPrice": price,
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": ts
        }
        
        query_string = urllib.parse.urlencode(sorted(params.items()))
        signature = hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
        full_url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?{query_string}&signature={signature}"
        
        headers = {"X-BX-APIKEY": API_KEY}
        return requests.post(full_url, headers=headers)

    # TP und SL nacheinander senden
    r_tp = place_order(tp_p, "TAKE_PROFIT_MARKET")
    # STOP_LOSS_MARKET durch STOP_MARKET ersetzt!
    r_sl = place_order(sl_p, "STOP_MARKET") 

    print(f"[API] TP: {r_tp.json().get('msg')} | SL: {r_sl.json().get('msg')}")

# ---------------- MAIN LOGIC (KORRIGIERTES TIMING) ----------------

def execute_trade_bingx(symbol):
    ohlcv = get_ohlcv(symbol, RSI_TIMEFRAME)
    if not ohlcv: return
    rsi = calc_rsi([float(c["close"]) for c in ohlcv])
    if rsi < 80:
        print(f"[RSI BLOCK] {symbol} RSI={rsi:.1f} < 80")
        return

    price = get_price_bingx(symbol)
    if not price: return

    trade_size_usdt, leverage = 10, 10
    qty = round(trade_size_usdt / price, 6)

    # Entry Order senden
    entry_params = {
        "symbol": symbol, "side": "SELL", "positionSide": "SHORT",
        "type": "MARKET", "quantity": str(qty), "leverage": str(leverage),
        "timestamp": str(int(time.time() * 1000))
    }
    entry_params["signature"] = sign_bingx(entry_params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=entry_params, headers={"X-BX-APIKEY": API_KEY})

    # Warten, bis die Position im BingX System registriert ist
    time.sleep(2) 
    
    # TP/SL Berechnung
    tp = price * (1 - TP_PERCENT / 100)
    sl = price * (1 + SL_PERCENT / 100)
    be_trigger = price * (1 - BE_PERCENT / 100)

    set_tp_sl(symbol, qty, tp, sl)
    threading.Thread(target=monitor_be, args=(symbol, qty, price, tp, be_trigger)).start()

# ---------------- BE MONITOR ----------------

def monitor_be(symbol, qty, entry, tp, trigger):
    while is_pos_open_bingx(symbol):
        curr = get_price_bingx(symbol)
        if curr and curr <= trigger:
            ts = str(int(time.time() * 1000))
            c_params = {"symbol": symbol, "timestamp": ts}
            c_params["signature"] = sign_bingx(c_params)
            requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/cancelAllOrders", data=c_params, headers={"X-BX-APIKEY": API_KEY})
            
            time.sleep(1)
            set_tp_sl(symbol, qty, tp, entry * 0.9995)
            print(f"[BE] SL auf Entry für {symbol} verschoben.")
            break
        time.sleep(3)

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
