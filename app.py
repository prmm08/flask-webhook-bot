import time
import hmac
import hashlib
import requests
import os
from flask import Flask, request, jsonify

API_KEY = "XeyESAWMvOPHPPlteKkem15yGzEPvHauxKj5LORpjrvOipxPza5DiWkGSMJGhWZyIKp0ZNQwhN17R3aon1RA"
API_SECRET = "EKHC1rgjFzQVBO9noJa1CHaeoh9vJqv78EXg76aqozvejJbTknkaVr2G3fJyUcBZs1rCoSRA5vMQ6gZYmIg"
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

def sign_params(params):
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

@app.route("/testorder", methods=["POST"])
def test_order():
    try:
        data = request.get_json(force=True)

        # Basisparameter
        currency = str(data.get("currency", "BTC")).upper()
        symbol = f"{currency}-USDT"
        side = str(data.get("side", "BUY")).upper()
        ORDER_SIZE_USDT = float(data.get("ORDER_SIZE_USDT", 10))
        ORDER_LEVERAGE = int(data.get("ORDER_LEVERAGE", 5))
        TP_PERCENT = float(data.get("TP_PERCENT", 2.0))
        SL_PERCENT = float(data.get("SL_PERCENT", 1.0))

        # Preis holen
        url_price = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url_price, params={"symbol": symbol}, timeout=10)
        price = float(r.json()["data"]["price"])
        qty = round(ORDER_SIZE_USDT / price, 6)

        headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}

        # Entry Limit Order
        url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
        entry_params = {
            "leverage": str(ORDER_LEVERAGE),
            "positionSide": "LONG" if side == "BUY" else "SHORT",
            "quantity": str(qty),
            "side": side,
            "symbol": symbol,
            "price": str(price),
            "timestamp": str(int(time.time() * 1000)),
            "type": "LIMIT",
            "timeInForce": "GTC"
        }
        entry_params["signature"] = sign_params(entry_params)
        entry_resp = requests.post(url_order, data=entry_params, headers=headers, timeout=10)

        # TP/SL Preise berechnen
        if side == "BUY":
            tp_price = round(price * (1 + TP_PERCENT / 100), 2)
            sl_price = round(price * (1 - SL_PERCENT / 100), 2)
            tp_side = "SELL"
            sl_side = "SELL"
        else:  # SELL/SHORT
            tp_price = round(price * (1 - TP_PERCENT / 100), 2)
            sl_price = round(price * (1 + SL_PERCENT / 100), 2)
            tp_side = "BUY"
            sl_side = "BUY"

        # TP Limit Order
        tp_params = {
            "symbol": symbol,
            "side": tp_side,
            "quantity": str(qty),
            "price": str(tp_price),
            "timestamp": str(int(time.time() * 1000)),
            "type": "LIMIT",
            "timeInForce": "GTC"
        }
        tp_params["signature"] = sign_params(tp_params)
        tp_resp = requests.post(url_order, data=tp_params, headers=headers, timeout=10)

        # SL Limit Order
        sl_params = {
            "symbol": symbol,
            "side": sl_side,
            "quantity": str(qty),
            "price": str(sl_price),
            "timestamp": str(int(time.time() * 1000)),
            "type": "LIMIT",
            "timeInForce": "GTC"
        }
        sl_params["signature"] = sign_params(sl_params)
        sl_resp = requests.post(url_order, data=sl_params, headers=headers, timeout=10)

        return jsonify({
            "status": "ok",
            "currency": currency,
            "side": side,
            "entry_price": price,
            "ORDER_SIZE_USDT": ORDER_SIZE_USDT,
            "ORDER_LEVERAGE": ORDER_LEVERAGE,
            "TP_PERCENT": TP_PERCENT,
            "SL_PERCENT": SL_PERCENT,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "entry_order_response": entry_resp.json(),
            "tp_order_response": tp_resp.json(),
            "sl_order_response": sl_resp.json()
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
