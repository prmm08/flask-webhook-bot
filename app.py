import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# -------- API Keys --------
API_KEY = "XeyESAWMvOPHPPlteKkem15yGzEPvHauxKj5LORpjrvOipxPza5DiWkGSMJGhWZyIKp0ZNQwhN17R3aon1RA"
API_SECRET = "EKHC1rgjFzQVBO9noJa1CHaeoh9vJqv78EXg76aqozvejJbTknkaVr2G3fJyUcBZs1rCoSRA5vMQ6gZYmIg"
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

def sign_params(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_price(symbol="BTC-USDT"):
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    return float(r.json()["data"]["price"])

def get_positions():
    """Fragt aktive Positionen ab"""
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {
        "timestamp": str(int(time.time() * 1000))
    }
    params["signature"] = sign_params(params)
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    return resp.json()

def close_all_positions(symbol):
    """Schließt alle offenen Positionen für ein Symbol"""
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}

    params = {
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000))
    }
    params["signature"] = sign_params(params)
    resp = requests.post(url, data=params, headers=headers, timeout=10)
    print("CloseAll response:", resp.json())
    return resp.json()

def monitor_position(symbol, position_side, entry_price, tp_price, sl_price, interval=5):
    """Überwacht Preis und schließt Position bei TP oder SL"""
    print(f"Monitoring {symbol} {position_side}... TP={tp_price}, SL={sl_price}")
    while True:
        current = get_price(symbol)
        print("Current price:", current)

        if position_side == "LONG":
            if current >= tp_price or current <= sl_price:
                print("Target reached, closing LONG position")
                close_all_positions(symbol)
                break
        elif position_side == "SHORT":
            if current <= tp_price or current >= sl_price:
                print("Target reached, closing SHORT position")
                close_all_positions(symbol)
                break

        time.sleep(interval)

@app.route("/alert", methods=["POST"])
def handle_alert():
    try:
        data = request.get_json(force=True)

        # Wichtige Felder aus dem Alert
        currency = str(data.get("currency", "")).upper()
        symbol = f"{currency}-USDT"   # BingX Symbol Standard

        # Default Parameter für SELL Order
        side = "SELL"
        size = 25          # USDT Notional
        leverage = 25
        tp_percent = 2   # Take Profit %
        sl_percent = 100   # Stop Loss %

        # Preis holen
        price = get_price(symbol)
        qty = round(size / price, 6)

        headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
        url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"

        # Entry Market Order
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

        # TP/SL Preise berechnen
        tp_price = round(price * (1 - tp_percent / 100), 2)
        sl_price = round(price * (1 + sl_percent / 100), 2)

        # Hintergrundthread starten
        threading.Thread(
            target=monitor_position,
            args=(symbol, "SHORT", price, tp_price, sl_price)
        ).start()

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
