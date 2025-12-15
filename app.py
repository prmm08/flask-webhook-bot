# -------- V 2.7: BINGX FUTURES ONLY - VERIFIED WEBHOOK + NO TREND FILTER --------

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

active_monitors = {}

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
    print(f"[ORDER] SHORT {symbol}")

    price = get_price_bingx(symbol)
    if not price:
        print("[ERROR] Preis konnte nicht geladen werden")
        return

    trade_size_usdt = 20
    leverage = 20

    tp_percent = 0.25
    sl_percent = 0.25

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

    print(f"[MONITOR] {symbol} | Entry={entry} TP={tp} SL={sl}")

    be_trigger = entry * 0.98
    be_set = False

    try:
        while True:
            curr = get_price_bingx(symbol)
            if not curr:
                time.sleep(1)
                continue

            if not be_set and curr <= be_trigger:
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

# ---------------- HEALTH CHECK ----------------

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"}), 200

# ---------------- WEBHOOK (GET + POST) ----------------

@app.route("/testorder", methods=["GET", "POST"])
def handle_alert():

    # GET → Verifizierung
    if request.method == "GET":
        return jsonify({"status": "ok", "message": "webhook active"}), 200

    # POST → cryptocurrencyalerting.com sendet oft KEIN JSON beim Test
    data = request.get_json(silent=True)

    # Wenn kein JSON → trotzdem 200 zurückgeben
    if not data:
        print("[INFO] Empty POST received (verification)")
        return jsonify({"status": "ok", "message": "post received"}), 200

    # Ab hier nur echte Signale
    currency = str(data.get("currency", "")).upper()
    if not currency:
        return jsonify({"error": "no currency"}), 400

    symbol = f"{currency}-USDT"
    print(f"[SIGNAL] {symbol}")

    if is_pos_open_bingx(symbol) or active_monitors.get(f"BINGX_{symbol}"):
        return jsonify({"status": "already_active"}), 200

    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()

    return jsonify({"status": "short_started", "symbol": symbol}), 200


# ---------------- ANTI-SLEEP PING ----------------

def keep_alive():
    while True:
        try:
            requests.get("https://flask-webhook-bot-1.onrender.com/")
        except:
            pass
        time.sleep(60)

threading.Thread(target=keep_alive, daemon=True).start()

# ---------------- START ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
