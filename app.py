# -------- V 4.1: BINGX LONG & SHORT + NUR BTC TREND FILTER (OHNE RSI FILTER) --------

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
RSI_TIMEFRAME = "1m" # Wird nur noch zur Info geladen
BTC_TREND_TIMEFRAME = "1h"
MA_PERIOD = 50
TP_PERCENT, SL_PERCENT, BE_PERCENT = 3.0, 1.5, 0.5

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

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except: return []

# ---------------- INDICATORS ----------------

def calc_ma(closes, period):
    if len(closes) < period: return closes[-1]
    return sum(closes[-period:]) / period

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

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
    pos = get_active_position(symbol)
    if not pos: return
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions?{qs}&signature={sig}"
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print(f"[EXIT] {symbol} Markt-Close ausgeführt ({pos.get('positionSide')}).")

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

# ---------------- MONITORING ----------------

def monitor_trade(symbol, entry, tp, sl, be_trigger, side):
    be_active = False
    while get_active_position(symbol):
        curr = get_price_bingx(symbol)
        if not curr: time.sleep(2); continue

        profit_reached = (side == "LONG" and curr >= be_trigger) or (side == "SHORT" and curr <= be_trigger)

        if not be_active and profit_reached:
            be_active = True
            print(f"[BE STATUS] {symbol} Profit erreicht. BE-Schutz ist jetzt scharf.")

        back_to_entry = (side == "LONG" and curr <= entry) or (side == "SHORT" and curr >= entry)
        if be_active and back_to_entry:
            print(f"[BE EXIT] {symbol} Kurs zurück am Entry. Schließe Position.")
            close_position_market(symbol)
            break
        
        tp_hit = (side == "LONG" and curr >= tp) or (side == "SHORT" and curr <= tp)
        sl_hit = (side == "LONG" and curr <= sl) or (side == "SHORT" and curr >= sl)

        if tp_hit or sl_hit:
            reason = "TP" if tp_hit else "SL"
            print(f"[LOCAL EXIT] {symbol} {reason} erreicht. Schließe...")
            close_position_market(symbol)
            break

        time.sleep(3)
    print(f"[MONITOR] Ende für {symbol}")

# ---------------- EXECUTION LOGIC (OHNE RSI FILTER) ----------------

def execute_trade_bingx(symbol):
    # 1. BTC Trend Check (Master Filter)
    ohlcv_btc = get_ohlcv("BTC-USDT", BTC_TREND_TIMEFRAME, limit=MA_PERIOD + 1)
    if not ohlcv_btc: return
    closes_btc = [float(c["close"]) for c in ohlcv_btc]
    ma_btc = calc_ma(closes_btc, MA_PERIOD)
    current_btc_price = closes_btc[-1]

    trade_side = None
    if current_btc_price > ma_btc:
        trade_side = "LONG"
    elif current_btc_price < ma_btc:
        trade_side = "SHORT"
    
    if not trade_side:
        print("[TREND FILTER] BTC Trend unklar (Seitwärts), kein Trade.")
        return

    # Info: RSI wird nur noch geladen, um es im Log anzuzeigen
    ohlcv_asset = get_ohlcv(symbol, RSI_TIMEFRAME)
    rsi = calc_rsi([float(c["close"]) for c in ohlcv_asset]) if ohlcv_asset else 50
    
    # 2. Order Ausführung, da Filter passiert
    price = get_price_bingx(symbol)
    if not price: return
    
    trade_size_usdt, leverage = 10, 10
    qty = round(trade_size_usdt / price, 6)
    order_side = "BUY" if trade_side == "LONG" else "SELL"
    
    print(f"[ENTRY] {trade_side} {symbol} @ {price} | BTC-Trend: {trade_side} | RSI: {rsi:.1f}")

    ts = str(int(time.time() * 1000))
    entry_p = {"symbol": symbol, "side": order_side, "positionSide": trade_side, "type": "MARKET", "quantity": str(qty), "leverage": str(leverage), "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(entry_p.items()))
    sig = sign_bingx(entry_p)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})

    time.sleep(2)
    
    # Kalkulationen TP/SL/BE dynamisch für Long/Short
    tp = price * (1 + TP_PERCENT / 100) if trade_side == "LONG" else price * (1 - TP_PERCENT / 100)
    sl = price * (1 - SL_PERCENT / 100) if trade_side == "LONG" else price * (1 + SL_PERCENT / 100)
    be_trigger = price * (1 + BE_PERCENT / 100) if trade_side == "LONG" else price * (1 - BE_PERCENT / 100)
    
    set_tp_sl(symbol, qty, tp, sl, trade_side)
    threading.Thread(target=monitor_trade, args=(symbol, price, tp, sl, be_trigger, trade_side)).start()

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
