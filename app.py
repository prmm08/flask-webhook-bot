# -------- VER 1.7: Auto Orders/TP/SL/Monitoring/Cooldown/BE --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

def sign_params(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_price(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    return float(r.json()["data"]["price"])

def get_positions():
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {"timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_params(params)
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    return resp.json()

def close_all_positions(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_params(params)
    resp = requests.post(url, data=params, headers=headers, timeout=10)
    print("CloseAll response:", resp.json())
    return resp.json()

def dynamic_round(price, value):
    if price > 1000:
        decimals = 2
    elif price > 1:
        decimals = 4
    else:
        decimals = 6
    return round(value, decimals)

active_monitors = {}

def monitor_position(symbol, entry_price, tp_price, sl_price, interval=1):
    """Überwacht SHORT-Position mit einmaligem BE-TSL"""
    print(f"Monitoring SHORT {symbol}... TP={tp_price}, SL={sl_price}")
    active_monitors[symbol] = True
    try:
        trailing_percent = 0.025  # 2%
        be_set = False

        while True:
            current = get_price(symbol)
            print(f"Current {symbol} price:", current)

            # Break-Even setzen bei +2% Gewinn
            if not be_set and current <= entry_price * (1 - trailing_percent):
                sl_price = entry_price
                be_set = True
                print(f"BE aktiviert für SHORT {symbol}: SL={sl_price}")

            # Schließen bei TP oder SL
            if current <= tp_price or current >= sl_price:
                print(f"Target reached, closing SHORT {symbol} position")
                close_all_positions(symbol)
                break

            time.sleep(interval)
    finally:
        active_monitors[symbol] = False

cooldowns = {}
COOLDOWN_SECONDS = 2 * 60 * 60

@app.route("/", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "ok", "message": "Webhook erreichbar"}), 200

@app.route("/testorder", methods=["POST"])
def handle_alert():
    try:
        data = request.get_json(force=True, silent=True) or {}
        if not data.get("currency"):
            return jsonify({"status": "ok", "message": "Webhook erreichbar"}), 200

        currency = str(data.get("currency", "")).upper()
        symbol = f"{currency}-USDT"

        now = time.time()
        last_exec = cooldowns.get(symbol, 0)
        if now - last_exec < COOLDOWN_SECONDS:
            return jsonify({
                "status": "cooldown",
                "message": f"{symbol} ist noch im Cooldown, bitte warten.",
                "remaining_seconds": int(COOLDOWN_SECONDS - (now - last_exec))
            }), 200

        side = "SELL"
        size = 40
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

        tp_price = dynamic_round(price, price * (1 - tp_percent / 100))
        sl_price = dynamic_round(price, price * (1 + sl_percent / 100))

        if not active_monitors.get(symbol, False):
            threading.Thread(
                target=monitor_position,
                args=(symbol, price, tp_price, sl_price)
            ).start()

        cooldowns[symbol] = now

        return jsonify({
            "status": "ok",
            "alert_received": data,
            "symbol": symbol,
            "side": side,
            "entry_price": price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "entry_response": entry_resp.json(),
            "positions_response": get_positions()
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

