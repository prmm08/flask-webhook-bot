# -------- V 2.8: BINGX FUTURES ONLY - SHORT ONLY + RSI FILTER + CLEAN LOGS + VERIFIED WEBHOOK --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# --- API Konfiguration BingX ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# --- Flask ohne Access Logs starten ---
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

active_monitors = {}

# --- RSI TIMEFRAME (wählbar: "1m", "5m", "15m") ---
RSI_TIMEFRAME = "1m"

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

# ---------------- OHLCV + RSI ----------------

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except:
        return []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50

    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    avg_gain = sum(gains) / period if gains else 0.00001
    avg_loss = sum(losses) / period if losses else 0.00001

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

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

    # --- RSI CHECK ---
    ohlcv = get_ohlcv(symbol, RSI_TIMEFRAME, 100)
    if not ohlcv:
        return

    closes = [float(c["close"]) for c in ohlcv]
    rsi = calc_rsi(closes)

    if rsi < 80:
        print(f"[RSI BLOCK] {symbol} RSI={rsi:.1f} ({RSI_TIMEFRAME}) < 80 → Kein SHORT")
        return

    # --- Preis laden ---
    price = get_price_bingx(symbol)
    if price is None:
        return

    print(f"[ORDER] SHORT {symbol} | Entry={price} | RSI={rsi:.1f} ({RSI_TIMEFRAME})")

    trade_size_usdt = 20
    leverage = 20

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
