import time
import hmac
import hashlib
import requests
import os
from flask import Flask, request, jsonify

# -------- API Keys --------
API_KEY = "XeyESAWMvOPHPPlteKkem15yGzEPvHauxKj5LORpjrvOipxPza5DiWkGSMJGhWZyIKp0ZNQwhN17R3aon1RA"
API_SECRET = "EKHC1rgjFzQVBO9noJa1CHaeoh9vJqv78EXg76aqozvejJbTknkaVr2G3fJyUcBZs1rCoSRA5vMQ6gZYmIg"

BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

def sign_params(params):
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

@app.route("/", methods=["GET"])
def home():
    return {"status": "Server läuft", "routes": ["/ping (GET)", "/testorder (POST)"]}

@app.route("/ping", methods=["GET"])
def ping_bingx():
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    resp = requests.get(url, params={"symbol": "BTC-USDT"}, timeout=10)
    return jsonify({"status": "ok", "bingx_response": resp.json()}), 200

# -------- Flexible Testorder mit TP/SL --------
@app.route("/testorder", methods=["POST"])
def test_order():
    try:
        data = request.get_json(force=True)

        symbol = str(data.get("symbol", "BTC-USDT")).upper()
        side = str(data.get("side", "SELL")).upper()
        size = float(data.get("size", 10))       # USDT Notional
        leverage = int(data.get("leverage", 5))
        tp_percent = float(data.get("tp_percent", 2.0))
        sl_percent = float(data.get("sl_percent", 1.0))

        # Preis holen
        url_price = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url_price, params={"symbol": symbol}, timeout=10)
        price = float(r.json()["data"]["price"])

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
            tp_side = "SELL"
            sl_side = "SELL"
        else:  # SELL
            tp_price = round(price * (1 - tp_percent / 100), 2)
            sl_price = round(price * (1 + sl_percent / 100), 2)
            tp_side = "BUY"
            sl_side = "BUY"

        # TP Conditional Order
        tp_params = {
            "symbol": symbol,
            "side": tp_side,
            "quantity": str(qty),
            "stopPrice": str(tp_price),
            "timestamp": str(int(time.time() * 1000)),
            "type": "TAKE_PROFIT_MARKET"
        }
        tp_params["signature"] = sign_params(tp_params)
        tp_resp = requests.post(url_order, data=tp_params, headers=headers, timeout=10)

        # SL Conditional Order
        sl_params = {
            "symbol": symbol,
            "side": sl_side,
            "quantity": str(qty),
            "stopPrice": str(sl_price),
            "timestamp": str(int(time.time() * 1000)),
            "type": "STOP_MARKET"
        }
        sl_params["signature"] = sign_params(sl_params)
        sl_resp = requests.post(url_order, data=sl_params, headers=headers, timeout=10)

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
            "tp_response": tp_resp.json(),
            "sl_response": sl_resp.json()
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
