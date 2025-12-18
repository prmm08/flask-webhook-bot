# -------- V 5.0: BINGX FUTURES - NUR LONG WENN PREIS > EMA 50 --------

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
EMA_TIMEFRAME = "1m"  # Zeitrahmen für den EMA
EMA_PERIOD = 50
TP_PERCENT, SL_PERCENT, BE_PERCENT = 0.5, 0.5, 0.5
TRADE_SIDE = "LONG"

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

def get_ohlcv(symbol, interval, limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except: return []

# ---------------- INDICATORS ----------------

def calc_ema(prices, period):
    if len(prices) < period: return prices[-1]
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # Start mit SMA
    for price in prices[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

# ---------------- POSITION ACTIONS ----------------

def get_active_position(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        for p in r.get("data", []):
             if float(p.get("positionAmt", 0)) != 0: return p 
        return None
    except: return None

def close_position_market(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions?{qs}&signature={sig}"
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print(f"[EXIT] {symbol} Markt-Close ausgeführt.")

def set_tp_sl(symbol, qty, tp_price, sl_price):
    exit_side = "SELL" # Exit für Long ist Sell
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": exit_side, "positionSide": TRADE_SIDE,
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
    be_active = False
    while get_active_position(symbol):
        curr = get_price_bingx(symbol)
        if not curr: time.sleep(2); continue

        # BE-Trigger bei LONG: Preis steigt über Trigger
        if not be_active and curr >= be_trigger:
            be_active = True
            print(f"[BE STATUS] {symbol} Profit-Schwelle erreicht. BE-Schutz aktiv.")

        # BE-Exit: Kurs fällt zurück auf Entry (oder tiefer)
        if be_active and curr <= entry:
            print(f"[BE EXIT] {symbol} zurück am Entry. Schließe Position.")
            close_position_market(symbol)
            break
        
        # Lokale Sicherheits-Checks
        if curr >= tp or curr <= sl:
            print(f"[LOCAL EXIT] {symbol} TP/SL erreicht.")
            close_position_market(symbol)
            break

        time.sleep(3)

# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol):
    # EMA Berechnung
    ohlcv = get_ohlcv(symbol, EMA_TIMEFRAME, limit=100)
    if not ohlcv: return
    closes = [float(c["close"]) for c in ohlcv]
    ema_value = calc_ema(closes, EMA_PERIOD)
    current_price = closes[-1]
    
    # NEU: Filterbedingung Preis > EMA 50
    if current_price <= ema_value:
        print(f"[EMA BLOCK] {symbol} Preis ({current_price}) <= EMA {EMA_PERIOD} ({ema_value:.6f})")
        return

    trade_size_usdt, leverage = 10, 10
    qty = round(trade_size_usdt / current_price, 6)
    
    print(f"[ENTRY] LONG {symbol} @ {current_price} | EMA50: {ema_value:.6f}")

    ts = str(int(time.time() * 1000))
    entry_p = {"symbol": symbol, "side": "BUY", "positionSide": TRADE_SIDE, "type": "MARKET", "quantity": str(qty), "leverage": str(leverage), "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(entry_p.items()))
    sig = sign_bingx(entry_p)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})

    time.sleep(2)
    
    tp = current_price * (1 + TP_PERCENT / 100)
    sl = current_price * (1 - SL_PERCENT / 100)
    be_trigger = current_price * (1 + BE_PERCENT / 100)
    
    set_tp_sl(symbol, qty, tp, sl)
    threading.Thread(target=monitor_trade, args=(symbol, current_price, tp, sl, be_trigger)).start()

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET": return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"status": "ignored"}), 200
    symbol = f"{currency}-USDT"
    
    if not get_active_position(symbol):
        threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()
        return jsonify({"status": "started", "symbol": symbol}), 200
    return jsonify({"status": "active"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
