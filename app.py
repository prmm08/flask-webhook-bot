import time
import hmac
import hashlib
import requests
import os
from flask import Flask, jsonify

# -------- API Keys --------
API_KEY = "DEIN_API_KEY"
API_SECRET = "DEIN_API_SECRET"

BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return {"status": "Server läuft", "routes": ["/ping (GET)", "/testorder (GET)"]}

# -------- Verbindungstest --------
@app.route("/ping", methods=["GET"])
def ping_bingx():
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        resp = requests.get(url, params={"symbol": "BTC-USDT"}, timeout=10)
        return jsonify({
            "status": "ok",
            "bingx_status_code": resp.status_code,
            "bingx_response": resp.json()
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

# -------- Testorder --------
@app.route("/testorder", methods=["GET"])
def test_order():
    try:
        url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
        params = {
            "leverage": "5",
            "positionSide": "SHORT",   # SELL → SHORT
            "quantity": "0.0001",      # Mini-Menge zum Testen
            "side": "SELL",
            "symbol": "BTC-USDT",
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
            "bingx_status_code": resp.status_code,
            "bingx_response": resp.json(),
            "final_payload": params
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Render setzt $PORT automatisch
    app.run(host="0.0.0.0", port=port)
