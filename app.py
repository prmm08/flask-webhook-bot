# -------- VER 2.0: Minimal SHORT Bot (No Filters) --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# ---------------- SIGNING ----------------

def sign_params(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ---------------- PRICE ----------------

def get_price(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    return float(r.json()["data"]["price"])

# ---------------- CLOSE ALL ----------------

def close_all_positions(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_params(params)
    resp = requests.post(url, data=params, headers=headers, timeout=10)
    print("[CLOSE]", resp.json())
    return resp.json()

# ---------------- DYNAMIC ROUND ----------------

def dynamic_round(price, value):
    if price > 1000:
        decimals = 2
    elif price > 1:
        decimals = 4
    else:
        decimals = 6
    return round(value, decimals)

# ---------------- SHORT AUSLÖSEN ----------------

active_monitors = {}

def trigger_short(symbol):
    side = "SELL"
    size = 100
    leverage = 20
    tp_percent = 5
    sl_percent = 2

    price = get_price(symbol)
    qty = round(size / price, 6)

    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"

    entry_params = {
        "leverage": str(leverage),
        "positionSide": "SHORT",
        "quantity": str(qty),
        "side": side,
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    entry_params["signature"] = sign_params(entry_params)
    entry_resp = requests.post(url_order, data=entry_params, headers=headers, timeout=10)

    print("[SHORT OPEN]", entry_resp.json())

    tp_price = dynamic_round(price, price * 0.95)
    sl_price = dynamic_round(price, price * 1.02)

    if not active_monitors.get(symbol, False):
        threading.Thread(
            target=monitor_position,
            args=(symbol, price, tp_price, sl_price)
        ).start()

# ---------------- POSITION MONITOR ----------------

def monitor_position(symbol, entry_price, tp_price, sl_price, interval=1):
    print(f"[MONITOR] {symbol} gestartet")
    active_monitors[symbol] = True
    try:
        trailing_percent = 0.025
        be_set = False

        while True:
            current = get_price(symbol)
            print(f"[PRICE] {symbol} = {current}")

            # Break-Even
            if not be_set and current <= entry_price * (1 - trailing_percent):
                sl_price = entry_price
                be_set = True
                print(f"[BE] Break-Even aktiviert für {symbol}")

            # TP oder SL
            if current <= tp_price or current >= sl_price:
                print(f"[EXIT] {symbol} TP/SL erreicht")
                close_all_positions(symbol)
                break

            time.sleep(interval)
    finally:
        active_monitors[symbol] = False

# ---------------- WEBHOOK ----------------

@app.route("/signal", methods=["POST"])
def handle_signal():
    try:
        data = request.get_json(force=True)
        print("[JSON]", data)

        if not data or "currency" not in data:
            return jsonify({"status": "ignored", "reason": "Ungültiges JSON"}), 200

        currency = str(data["currency"]).upper()
        symbol = f"{currency}-USDT"

        print(f"[RECEIVED] SHORT SIGNAL für {symbol}")

        trigger_short(symbol)

        return jsonify({"status": "ok", "message": "SHORT ausgeführt"}), 200

    except Exception as e:
        print("[ERROR]", e)
        return jsonify({"status": "error", "message": str(e)}), 400

# ---------------- RUN ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
