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

@app.route("/", methods=["GET"])
def home():
    return {"status": "Server läuft", "routes": ["/ping (GET)", "/testorder (POST)"]}

# -------- Verbindungstest --------
@app.route("/ping", methods=["GET"])
def ping_bingx():
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    resp = requests.get(url, params={"symbol": "BTC-USDT"}, timeout=10)
    return jsonify({"status": "ok", "bingx_response": resp.json()}), 200

# -------- Flexible Testorder --------
@app.route("/testorder", methods=["POST"])
def test_order():
    try:
        data = request.get_json(force=True)

        symbol = str(data.get("symbol", "BTC-USDT")).upper()
        side = str(data.get("side", "SELL")).upper()
        size = float(data.get("size", 10))       # USDT Notional
        leverage = int(data.get("leverage", 5))

        # Preis holen
        url_price = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url_price, params={"symbol": symbol}, timeout=10)
        price = float(r.json()["data"]["price"])

        qty = round(size / price, 6)

        url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
        params = {
            "leverage": str(leverage),
            "positionSide": "SHORT" if side == "SELL" else "LONG",
            "quantity": str(qty),
            "side": side,
            "symbol": symbol,
            "timestamp": str(int(time.time() * 1000)),
            "type": "MARKET"
        }

        # Signatur erstellen
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature

        headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
        resp = requests.post(url_order, data=params, headers=headers, timeout=10)

        return jsonify({
            "status": "ok",
            "received_signal": {"symbol": symbol, "side": side, "size": size, "leverage": leverage},
            "bingx_payload": params,
            "bingx_status_code": resp.status_code,
            "bingx_response": resp.json()
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
