import requests
import os
from flask import Flask, jsonify

BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return {"status": "Server läuft", "routes": ["/ping (GET)"]}

@app.route("/ping", methods=["GET"])
def ping_bingx():
    try:
        # einfacher Test: Preis für BTC-USDT abfragen
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        resp = requests.get(url, params={"symbol": "BTC-USDT"}, timeout=10)
        return jsonify({
            "status": "ok",
            "bingx_status_code": resp.status_code,
            "bingx_response": resp.json()
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Render setzt $PORT automatisch
    app.run(host="0.0.0.0", port=port)
