# -------- V 3.0: BINGX FUTURES ONLY - SHORT ONLY + ADX FILTER --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify
import logging

# --- API Konfiguration BingX ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# --- Flask Access Logs deaktivieren ---
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

active_monitors = {}

# --- ADX Timeframe ---
ADX_TIMEFRAME = "5m"

# ---------------- SIGNING ----------------

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ---------------- PRICE ----------------

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except:
        return None

# ---------------- OHLCV ----------------

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except:
        return []

# ---------------- ADX ----------------

def calc_adx(ohlcv, period=14):
    if len(ohlcv) < period + 2:
        return 20, 0, 0  # fallback

    highs = [float(c["high"]) for c in ohlcv]
    lows = [float(c["low"]) for c in ohlcv]
    closes = [float(c["close"]) for c in ohlcv]

    tr_list, plus_dm_list, minus_dm_list = [], [], []

    for i in range(1, len(ohlcv)):
        high, low = highs[i], lows[i]
        prev_close = closes[i - 1]

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        plus_dm_list.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm_list.append(down_move if down_move > up_move and down_move > 0 else 0)

    tr14 = sum(tr_list[-period:])
    plus_dm14 = sum(plus_dm_list[-period:])
    minus_dm14 = sum(minus_dm_list[-period:])

    plus_di = 100 * (plus_dm14 / tr14) if tr14 != 0 else 0
    minus_di = 100 * (minus_dm14 / tr14) if tr14 != 0 else 0

    dx = abs(plus_di - minus_di) / (plus_di + minus_di + 0.00001) * 100
    adx = dx  # simplified ADX

    return adx, plus_di, minus_di

# ---------------- POSITION CHECK ----------------

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(
            f"{BINGX_BASE}/openApi/swap/v2/user/positions",
            params=params,
            headers={"X-BX-APIKEY": API_KEY},
            timeout=10
        ).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except:
        return True

# ---------------- CLOSE ----------------

def close_bingx(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions",
        data=params,
        headers={"X-BX-APIKEY": API_KEY}
    )

# ---------------- SHORT ORDER ----------------

def execute_trade_bingx(symbol):

    # --- ADX ---
    ohlcv_adx = get_ohlcv(symbol, ADX_TIMEFRAME, 100)
    if not ohlcv_adx:
        return

    adx, plus_di, minus_di = calc_adx(ohlcv_adx)

    if not (adx > 25 and minus_di > plus_di):
        print(f"[ADX BLOCK] {symbol} ADX={adx:.1f} +DI={plus_di:.1f} -DI={minus_di:.1f}")
        return

    # --- Preis ---
    price = get_price_bingx(symbol)
    if price is None:
        return

    print(f"[ORDER] SHORT {symbol} | Entry={price} | ADX={adx:.1f}")

    trade_size_usdt = 10
    leverage = 10

    tp_percent = 1
    sl_percent = 1

    qty = round(trade_size_usdt / price, 6)

    params = {
        "leverage": str(leverage),
        "positionSide": "SHORT",
        "quantity": str(qty),
        "side": "SELL",
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)

    requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/order",
        data=params,
        headers={"X-BX-APIKEY": API_KEY},
        timeout=10
    )

    entry = price
    tp = entry * (1 - tp_percent / 100)
    sl = entry * (1 + sl_percent / 100)

    threading.Thread(target=monitor_position, args=(symbol, entry, tp, sl)).start()

# ---------------- MONITOR ----------------

def monitor_position(symbol, entry, tp, sl):
    key = f"BINGX_{symbol}"
    active_monitors[key] = True

    print(f"[MONITOR] {symbol} SHORT | Entry={entry} TP={tp} SL={sl}")

    be_trigger = entry * 1.02
    be_set = False

    try:
        while True:
            curr = get_price_bingx(symbol)
            if curr is None:
                time.sleep(1)
                continue

            if not be_set and curr >= be_trigger:
                sl = entry
                be_set = True
                print(f"[BE] {symbol} aktiviert")

            if curr <= tp or curr >= sl:
                reason = "TP" if curr <= tp else "SL/BE"
                print(f"[EXIT] {symbol} → {reason}")
                close_bingx(symbol)
                break

            time.sleep(1)

    finally:
        active_monitors[key] = False
        print(f"[MONITOR END] {symbol}")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["GET", "POST"])
def handle_alert():

    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "ok"}), 200

    currency = str(data.get("currency", "")).upper()

    if not currency:
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"
    print(f"[SIGNAL] {symbol}")

    if is_pos_open_bingx(symbol) or active_monitors.get(f"BINGX_{symbol}"):
        return jsonify({"status": "already_active"}), 200

    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()

    return jsonify({"status": "short_started", "symbol": symbol}), 200

# ---------------- ANTI-SLEEP ----------------

def keep_alive():
    while True:
        try:
            requests.get("https://flask-webhook-bot-1.onrender.com/testorder")
        except:
            pass
        time.sleep(60)

threading.Thread(target=keep_alive, daemon=True).start()

# ---------------- START ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
