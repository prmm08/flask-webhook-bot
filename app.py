# -------- V 4.2: BINGX LONG & SHORT + ADX & RSI FILTER (FIXED) --------

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

# Flask Logs reduzieren
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# Globale Settings
RSI_TIMEFRAME = "1m"
ADX_TIMEFRAME = "5m"
TP_PERCENT, SL_PERCENT, BE_PERCENT = 1.0, 1.5, 0.5

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

def get_ohlcv(symbol, interval, limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except: return []

# ---------------- INDICATORS ----------------

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_adx(ohlcv, period=14):
    if len(ohlcv) < period + 5: return 0, 0, 0
    highs = [float(c["high"]) for c in ohlcv]
    lows = [float(c["low"]) for c in ohlcv]
    closes = [float(c["close"]) for c in ohlcv]
    
    tr_l, p_dm_l, m_dm_l = [], [], []
    for i in range(1, len(ohlcv)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        tr_l.append(tr)
        up, down = highs[i]-highs[i-1], lows[i-1]-lows[i]
        p_dm_l.append(up if up > down and up > 0 else 0)
        m_dm_l.append(down if down > up and down > 0 else 0)

    tr14 = sum(tr_l[-period:])
    p_di = 100 * (sum(p_dm_l[-period:]) / tr14) if tr14 else 0
    m_di = 100 * (sum(m_dm_l[-period:]) / tr14) if tr14 else 0
    dx = abs(p_di - m_di) / (p_di + m_di + 0.001) * 100
    return dx, p_di, m_di

# ---------------- POSITION CHECK ----------------

def get_active_position(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}).json()
        for p in r.get("data", []):
            if float(p.get("positionAmt", 0)) != 0:
                return p 
        return None
    except: return None

# ---------------- ORDER LOGIC ----------------

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
        return {"msg": "Failed"}

    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

def execute_trade(symbol):
    ohlcv_adx = get_ohlcv(symbol, ADX_TIMEFRAME)
    adx, p_di, m_di = calc_adx(ohlcv_adx)
    
    if adx < 25:
        print(f"[ADX BLOCK] {symbol} ADX={adx:.1f} < 25")
        return

    ohlcv_rsi = get_ohlcv(symbol, RSI_TIMEFRAME)
    rsi = calc_rsi([float(c["close"]) for c in ohlcv_rsi])

    side = None
    if rsi < 30 and p_di > m_di:
        side = "LONG"
    elif rsi > 70 and m_di > p_di:
        side = "SHORT"
    
    if not side:
        print(f"[FILTER BLOCK] {symbol} RSI={rsi:.1f} ADX={adx:.1f}")
        return

    order_side = "BUY" if side == "LONG" else "SELL"
    price = get_price_bingx(symbol)
    if not price: return

    trade_size, leverage = 10, 10
    qty = round(trade_size / price, 6)
    
    print(f"[SIGNAL] {side} {symbol} | RSI={rsi:.1f} | ADX={adx:.1f}")

    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol, "side": order_side, "positionSide": side,
        "type": "MARKET", "quantity": str(qty), "leverage": str(leverage), "timestamp": ts
    }
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})

    time.sleep(2) # Warte auf Position-Eröffnung
    
    tp = price * (1 + TP_PERCENT/100) if side == "LONG" else price * (1 - TP_PERCENT/100)
    sl = price * (1 - SL_PERCENT/100) if side == "LONG" else price * (1 + SL_PERCENT/100)
    be_trig = price * (1 + BE_PERCENT/100) if side == "LONG" else price * (1 - BE_PERCENT/100)

    set_tp_sl(symbol, qty, tp, sl, side)
    threading.Thread(target=monitor_be, args=(symbol, qty, price, tp, be_trig, side)).start()

# ---------------- BREAK-EVEN MONITOR ----------------

def monitor_be(symbol, qty, entry, tp, trigger, side):
    def cancel_tp_sl_orders(s):
        ts = str(int(time.time() * 1000))
        p = {"symbol": s, "timestamp": ts}
        qs = urllib.parse.urlencode(sorted(p.items()))
        sig = sign_bingx(p)
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/cancelAllOrders?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})
        
    while True:
        curr = get_price_bingx(symbol)
        if not curr: 
            time.sleep(3)
            continue
        
        is_triggered = (side == "LONG" and curr >= trigger) or (side == "SHORT" and curr <= trigger)
        
        if is_triggered:
            cancel_tp_sl_orders(symbol)
            time.sleep(1.5)
            be_level = entry * 1.0005 if side == "LONG" else entry * 0.9995
            set_tp_sl(symbol, qty, tp, be_level, side)
            print(f"[BE SUCCESS] {symbol} {side} SL auf BE verschoben.")
            break
        
        if not get_active_position(symbol): break
        time.sleep(5)

# ---------------- WEBHOOK ----------------

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
    return jsonify({"status": "position_exists"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
