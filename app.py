# -------- VER 2.1: Auto SHORT Orders / TP / SL / BE / Monitoring (mit robustem Entry-Fix) --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# -------- API Keys --------
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# -------- Signatur --------
def sign_params(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# -------- Mark-Price (Fix 1) --------
def get_price(symbol):
    """Holt den echten Futures-Mark-Preis."""
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/markPrice"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    return float(r.json()["data"]["markPrice"])

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

# -------- Dynamische Rundung --------
def dynamic_round(price, value):
    if price > 1000:
        decimals = 2
    elif price > 1:
        decimals = 4
    else:
        decimals = 6
    return round(value, decimals)

# -------- Monitor --------
active_monitors = {}

def monitor_position(symbol, entry_price, tp_price, sl_price, interval=1):
    print(f"[MONITOR] {symbol} SHORT gestartet | Entry={entry_price}, TP={tp_price}, SL={sl_price}")
    active_monitors[symbol] = True

    # -------- Fix 2: Dynamischer Spread --------
    initial_mark = get_price(symbol)
    spread = abs(initial_mark - entry_price)

    be_trigger = entry_price * 0.98 - spread
    be_set = False

    try:
        while True:
            current = get_price(symbol)

            # --- Break-Even ---
            if not be_set and current <= be_trigger:
                sl_price = entry_price
                be_set = True
                print(f"[BE] {symbol} aktiviert → SL auf Entry gesetzt ({sl_price})")

            # --- Exit ---
            if current <= tp_price:
                print(f"[EXIT] {symbol} → TP erreicht @ {current}")
                close_all_positions(symbol)
                break

            if current >= sl_price:
                print(f"[EXIT] {symbol} → SL/BE erreicht @ {current}")
                close_all_positions(symbol)
                break

            time.sleep(interval)

    finally:
        active_monitors[symbol] = False
        print(f"[MONITOR] {symbol} beendet")

# -------- Health Check --------
@app.route("/", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "ok"}), 200

# -------- Webhook --------
@app.route("/testorder", methods=["POST"])
def handle_alert():
    try:
        data = request.get_json(force=True, silent=True) or {}

        if not data.get("currency"):
            return jsonify({"status": "ok"}), 200

        currency = str(data.get("currency", "")).upper()
        symbol = f"{currency}-USDT"

        # --- SHORT Order Setup ---
        side = "SELL"
        size = 20
        leverage = 20
        tp_percent = 1.0
        sl_percent = 1.0

        # Vorab-Preis für Menge
        pre_price = get_price(symbol)
        qty = round(size / pre_price, 6)

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
        try:
            entry_json = entry_resp.json()
        except Exception:
            print("RAW RESPONSE TEXT:", entry_resp.text)
            return jsonify({"status": "error", "message": "invalid_json", "raw": entry_resp.text}), 400

        print("FULL ORDER RESPONSE:", entry_json)



        # -------- Fix 3: Robuste Entry-Preis-Erkennung --------
        data_block = entry_json.get("data") or entry_json.get("order") or {}

        entry_price = None
        for key in ["avgPrice", "price", "executedPrice", "fillPrice", "dealAvgPrice"]:
            if key in data_block:
                try:
                    entry_price = float(data_block[key])
                    break
                except:
                    pass

        if entry_price is None:
            entry_price = pre_price

        print(f"[ENTRY] Preis erkannt: {entry_price}")

        # -------- TP/SL erst berechnen, dann runden --------
        raw_tp = entry_price * (1 - tp_percent / 100)
        raw_sl = entry_price * (1 + sl_percent / 100)

        tp_price = dynamic_round(entry_price, raw_tp)
        sl_price = dynamic_round(entry_price, raw_sl)

        print(f"[ORDER] SHORT {symbol} | Entry={entry_price}, TP={tp_price}, SL={sl_price}")

        # --- Monitor starten ---
        if not active_monitors.get(symbol, False):
            threading.Thread(
                target=monitor_position,
                args=(symbol, entry_price, tp_price, sl_price),
                daemon=True
            ).start()

        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "entry_response": entry_json
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
