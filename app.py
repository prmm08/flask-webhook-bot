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

def close_position(symbol, side, qty):
    """Send Market Order in opposite direction to close"""
    url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}

    close_side = "SELL" if side == "BUY" else "BUY"
    params = {
        "symbol": symbol,
        "side": close_side,
        "quantity": str(qty),
        "type": "MARKET",
        "timestamp": str(int(time.time() * 1000))
    }
    params["signature"] = sign_params(params)
    resp = requests.post(url_order, data=params, headers=headers, timeout=10)
    print("Close response:", resp.json())
    return resp.json()

def monitor_position(symbol, side, qty, entry_price, tp_price, sl_price, interval=5):
    """Background loop to monitor price and close position"""
    print(f"Monitoring {symbol} position... TP={tp_price}, SL={sl_price}")
    while True:
        current = get_price(symbol)
        print("Current price:", current)

        if side == "BUY":
            if current >= tp_price or current <= sl_price:
                print("Target reached, closing BUY position")
                close_position(symbol, side, qty)
                break
        else:  # SELL
            if current <= tp_price or current >= sl_price:
                print("Target reached, closing SELL position")
                close_position(symbol, side, qty)
                break

        time.sleep(interval)

@app.route("/testorder", methods=["POST"])
def test_order():
    try:
        data = request.get_json(force=True)

        symbol = str(data.get("symbol", "BTC-USDT")).upper()
        side = str(data.get("side", "SELL")).upper()
        size = float(data.get("size", 20))       # USDT Notional
        leverage = int(data.get("leverage", 20))
        tp_percent = float(data.get("tp_percent", 2.0))
        sl_percent = float(data.get("sl_percent", 1.0))

        # Preis holen
        price = get_price(symbol)
        qty = round(size / price, 6)

        headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
        url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"

        # Entry Market Order
        entry_params = {
            "leverage": str(leverage),
            "positionSide": "SHORT" if side == "SELL" else "LONG",
            "quantity": str(qty),
            "side": side,
            "symbol": symbol,
            "timestamp": str(int(time.time() * 1000)),
            "type": "MARKET"
        }
        entry_params["signature"] = sign_params(entry_params)
        entry_resp = requests.post(url_order, data=entry_params, headers=headers, timeout=10)

        # TP/SL Preise berechnen
        if side == "BUY":
            tp_price = round(price * (1 + tp_percent / 100), 2)
            sl_price = round(price * (1 - sl_percent / 100), 2)
        else:  # SELL
            tp_price = round(price * (1 - tp_percent / 100), 2)
            sl_price = round(price * (1 + sl_percent / 100), 2)

        # Hintergrundthread starten
        threading.Thread(target=monitor_position, args=(symbol, side, qty, price, tp_price, sl_price)).start()

        return jsonify({
            "status": "ok",
            "received_signal": {
                "symbol": symbol,
                "side": side,
                "size": size,
                "leverage": leverage,
                "tp_percent": tp_percent,
                "sl_percent": sl_percent
            },
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
