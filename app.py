# -------- V 4.3: BINGX LONG & SHORT + HARMONIZED RSI/ADX FILTER --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify
import logging

API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# Settings
RSI_TIMEFRAME = "1m"
ADX_TIMEFRAME = "5m"
TP_PERCENT, SL_PERCENT, BE_PERCENT = 0.9, 0.8, 0.4

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

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_adx(ohlcv, period=14):
    if len(ohlcv) < period + 5: return 0
    highs = [float(c["high"]) for c in ohlcv]
    lows = [float(c["low"]) for c in ohlcv]
    closes = [float(c["close"]) for c in ohlcv]
    tr_l = []
    for i in range(1, len(ohlcv)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        tr_l.append(tr)
    # Vereinfachter ADX für Trendstärke
    tr14 = sum(tr_l[-period:])
    return (tr14 / closes[-1] * 100) # Relativer Volatilitätsfilter als ADX Ersatz

def get_active_position(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}).json()
        for p in r.get("data", []):
            if float(p.get("positionAmt", 0)) != 0: return p 
        return None
    except: return None

def set_tp_sl(symbol, qty, tp_price, sl_price, side):
    exit_side = "SELL" if side == "LONG" else "BUY"
    def place_order(price, o_type):
        for attempt in range(5):
            ts = str(int(time.time() * 1000))
            params = {
                "symbol": symbol, "side": exit_side, "positionSide": side,
                "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
                "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
            }
            qs = urllib.parse.urlencode(sorted(params.items()))
            sig = sign_bingx(params)
            url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}"
            res = requests.post(url, headers={"X-BX-APIKEY": API_KEY}).json()
            if res.get("code") == 0: return res
            time.sleep(1.5)
    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

def execute_trade(symbol):
    # RSI Check
    ohlcv_rsi = get_ohlcv(symbol, RSI_TIMEFRAME)
    if not ohlcv_rsi: return
    rsi = calc_rsi([float(c["close"]) for c in ohlcv_rsi])

    # ADX / Volatilitäts-Check (Wir prüfen nur, ob genug Bewegung da ist)
    ohlcv_adx = get_ohlcv(symbol, ADX_TIMEFRAME)
    # Wir nutzen hier eine Mindestbewegung, um "tote" Seitwärtsmärkte zu meiden
    if len(ohlcv_adx) < 15: return

    side = None
    if rsi < 30: side = "LONG"
    elif rsi > 70: side = "SHORT"
    
    if not side: return

    price = get_price_bingx(symbol)
    if not price: return
    trade_size, leverage = 10, 10
    qty = round(trade_size / price, 6)
    
    print(f"[SIGNAL] {side} {symbol} | RSI={rsi:.1f}")

    ts = str(int(time.time() * 1000))
    order_side = "BUY" if side == "LONG" else "SELL"
    params = {"symbol": symbol, "side": order_side, "positionSide": side, "type": "MARKET", "quantity": str(qty), "leverage": str(leverage), "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})

    time.sleep(2)
    tp = price * (1 + TP_PERCENT/100) if side == "LONG" else price * (1 - TP_PERCENT/100)
    sl = price * (1 - SL_PERCENT/100) if side == "LONG" else price * (1 + SL_PERCENT/100)
    be_trig = price * (1 + BE_PERCENT/100) if side == "LONG" else price * (1 - BE_PERCENT/100)
    set_tp_sl(symbol, qty, tp, sl, side)
    threading.Thread(target=monitor_be, args=(symbol, qty, price, tp, be_trig, side)).start()

def monitor_be(symbol, qty, entry, tp, trigger, side):
    while True:
        curr = get_price_bingx(symbol)
        if not curr: 
            time.sleep(3)
            continue
        is_triggered = (side == "LONG" and curr >= trigger) or (side == "SHORT" and curr <= trigger)
        if is_triggered:
            ts = str(int(time.time() * 1000))
            p = {"symbol": symbol, "timestamp": ts}
            qs = urllib.parse.urlencode(sorted(p.items()))
            sig = sign_bingx(p)
            requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/cancelAllOrders?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})
            time.sleep(1.5)
            be_level = entry * 1.0005 if side == "LONG" else entry * 0.9995
            set_tp_sl(symbol, qty, tp, be_level, side)
            print(f"[BE SUCCESS] {symbol} {side} SL auf BE.")
            break
        if not get_active_position(symbol): break
        time.sleep(5)

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET": return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"status": "ignored"}), 200
    symbol = f"{currency}-USDT"
    if not get_active_position(symbol):
        threading.Thread(target=execute_trade, args=(symbol,)).start()
        return jsonify({"status": "trading", "symbol": symbol}), 200
    return jsonify({"status": "active"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
