# -------- V 3.1: BINGX DUAL EMA TREND FILTER (BTC & COIN) --------

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
DEFAULT_TF = "30m"  # Standard Timeframe für BTC und Coin
MA_PERIOD = 50
TP_PERCENT, SL_PERCENT, BE_PERCENT = 1.5, 1.0, 0.5

# Lock gegen Race Conditions bei schnellen aufeinanderfolgenden Webhooks
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

def get_ohlcv(symbol, interval, limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except: return []

def calc_ema(closes, period):
    if len(closes) < period: return closes[-1]
    alpha = 2 / (period + 1)
    ema = closes[0]
    for price in closes:
        ema = (price * alpha) + (ema * (1 - alpha))
    return ema

# ---------------- POSITION ACTIONS ----------------

def has_active_position(symbol):
    """ Prüft ob eine Position ODER eine offene Order existiert """
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
        return True # Im Zweifel True zurückgeben, um Doppel-Orders zu vermeiden

def close_position_market(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions?{qs}&signature={sig}"
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print(f"[EXIT] {symbol} Markt-Close ausgeführt.")

def set_tp_sl(symbol, qty, tp_price, sl_price, side):
    exit_side = "SELL" if side == "LONG" else "BUY"
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": exit_side, "positionSide": side,
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

def execute_trade_bingx(symbol, timeframe):
    with order_lock:
        # 1. Sicherheits-Check: Ist bereits eine Position offen?
        if has_active_position(symbol):
            print(f"[ABORT] Position für {symbol} existiert bereits.")
            return

        # 2. Trend Check BTC
        ohlcv_btc = get_ohlcv("BTC-USDT", timeframe, limit=MA_PERIOD + 20)
        if not ohlcv_btc: return
        closes_btc = [float(c["close"]) for c in ohlcv_btc]
        ema_btc = calc_ema(closes_btc, MA_PERIOD)
        price_btc = closes_btc[-1]

        # 3. Trend Check COIN
        ohlcv_coin = get_ohlcv(symbol, timeframe, limit=MA_PERIOD + 20)
        if not ohlcv_coin: return
        closes_coin = [float(c["close"]) for c in ohlcv_coin]
        ema_coin = calc_ema(closes_coin, MA_PERIOD)
        price_coin = closes_coin[-1]

        # 4. Dual EMA Bedingung prüfen
        trade_side = None
        if price_btc > ema_btc and price_coin > ema_coin:
            trade_side = "LONG"
        elif price_btc < ema_btc and price_coin < ema_coin:
            trade_side = "SHORT"

        if not trade_side:
            print(f"[FILTER] {symbol} Trends nicht synchron (BTC vs Coin). Kein Trade.")
            return

        # 5. Order Platzierung
        trade_size_usdt, leverage = 10, 10
        qty = round(trade_size_usdt / price_coin, 6)
        order_side = "BUY" if trade_side == "LONG" else "SELL"
        
        print(f"[ENTRY] {trade_side} {symbol} | BTC > EMA: {price_btc > ema_btc} | {symbol} > EMA: {price_coin > ema_coin}")

        ts = str(int(time.time() * 1000))
        entry_p = {"symbol": symbol, "side": order_side, "positionSide": trade_side, "type": "MARKET", "quantity": str(qty), "leverage": str(leverage), "timestamp": ts}
        qs = urllib.parse.urlencode(sorted(entry_p.items()))
        sig = sign_bingx(entry_p)
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})

        # Kurze Pause für API-Sync
        time.sleep(2)
        
        # TP/SL/BE Kalkulation
        tp = price_coin * (1 + TP_PERCENT/100) if trade_side == "LONG" else price_coin * (1 - TP_PERCENT/100)
        sl = price_coin * (1 - SL_PERCENT/100) if trade_side == "LONG" else price_coin * (1 + SL_PERCENT/100)
        set_tp_sl(symbol, qty, tp, sl, trade_side)

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    tf = data.get("tf", DEFAULT_TF) # Erlaubt "tf" im Webhook zu senden
    
    if not currency: 
        return jsonify({"status": "no_currency"}), 400
    
    symbol = f"{currency}-USDT"
    
    # Thread starten zur Ausführung
    threading.Thread(target=execute_trade_bingx, args=(symbol, tf)).start()
    
    return jsonify({"status": "processing", "symbol": symbol}), 200

if __name__ == "__main__":
    # Port 5000 ist Standard
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
