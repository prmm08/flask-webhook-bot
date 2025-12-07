import time
import hmac
import hashlib
import requests
import os
from flask import Flask, request, jsonify

API_KEY = "DEIN_API_KEY"
API_SECRET = "DEIN_API_SECRET"
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return {"status": "Server läuft", "routes": ["/ping (GET)", "/webhook (POST)"]}

@app.route("/ping", methods=["GET"])
def ping_bingx():
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    resp = requests.get(url, params={"symbol": "BTC-USDT"}, timeout=10)
    return jsonify({"status": "ok", "bingx_response": resp.json()}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)

        # Currency aus dem Alert
        currency = str(data.get("currency", "BTC")).upper()
        symbol = f"{currency}-USDT"

        # Trading Parameter im gewünschten Format
        ORDER_SIZE_USDT = float(data.get("ORDER_SIZE_USDT", 10))
        ORDER_LEVERAGE = int(data.get("ORDER_LEVERAGE", 5))
        TP_PERCENT = float(data.get("TP_PERCENT", 2.0))
        SL_PERCENT = float(data.get("SL_PERCENT", 100.0))
        side = str(data.get("side", "BUY")).upper()

        # Preis holen
        url_price = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url_price, params={"symbol": symbol}, timeout=10)
        price = float(r.json()["data"]["price"])
        qty = round(ORDER_SIZE_USDT / price, 6)

        # Order vorbereiten
        url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
        params = {
            "leverage": str(ORDER_LEVERAGE),
            "positionSide": "LONG" if side == "BUY" else "SHORT",
            "quantity": str(qty),
            "side": side,
            "symbol": symbol,
            "timestamp": str(int(time.time() * 1000)),
            "type": "MARKET"
        }

        # Signatur
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature

        headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
        resp = requests.post(url_order, data=params, headers=headers, timeout=10)

        return jsonify({
            "status": "ok",
            "received_currency": currency,
            "ORDER_SIZE_USDT": ORDER_SIZE_USDT,
            "ORDER_LEVERAGE": ORDER_LEVERAGE,
            "TP_PERCENT": TP_PERCENT,
            "SL_PERCENT": SL_PERCENT,
            "side": side,
            "bingx_payload": params,
            "bingx_response": resp.json()
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
