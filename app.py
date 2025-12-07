import time
import hmac
import hashlib
import logging
import requests
import os
from flask import Flask, request, jsonify

# -------- API Keys --------
API_KEY = "XeyESAWMvOPHPPlteKkem15yGzEPvHauxKj5LORpjrvOipxPza5DiWkGSMJGhWZyIKp0ZNQwhN17R3aon1RA"
API_SECRET = "EKHC1rgjFzQVBO9noJa1CHaeoh9vJqv78EXg76aqozvejJbTknkaVr2G3fJyUcBZs1rCoSRA5vMQ6gZYmIg"

# -------- Trading Parameter --------
ORDER_SIZE_USDT = 10
ORDER_LEVERAGE = 5

BINGX_BASE = "https://open-api.bingx.com"
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# -------- Utils --------
def ts_ms():
    return int(time.time() * 1000)

def sign_query(query_str: str, secret: str):
    return hmac.new(secret.encode(), query_str.encode(), hashlib.sha256).hexdigest()

def norm_symbol(sym: str) -> str:
    s = sym.upper().strip().replace("/", "-")
    return s if "-" in s else f"{s}-USDT"

# -------- BingX Order --------
def bingx_place_order(symbol: str, side: str, notional_usdt: float, leverage: int):
    # Preis holen
    url_price = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    r = requests.get(url_price, params={"symbol": symbol}, timeout=10)
    price = float(r.json()["data"]["price"])

    qty = round((notional_usdt / price), 6)

    url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
    params = {
        "leverage": str(leverage),
        "positionSide": "SHORT" if side.upper() == "SELL" else "LONG",
        "quantity": str(qty),
        "side": side.upper(),
        "symbol": symbol,
        "timestamp": str(ts_ms()),
        "type": "MARKET"
    }

    # Alphabetisch sortieren
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature = sign_query(query, API_SECRET)
    params["signature"] = signature

    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(url_order, data=params, headers=headers, timeout=10)
    logging.info(f"[BINGX] Response: {resp.status_code} {resp.text}")
    try:
        return {
            "status_code": resp.status_code,
            "json": resp.json(),
            "raw": resp.text,
            "final_payload": params   # <-- zeigt die finale Payload
        }
    except Exception:
        return {"status_code": resp.status_code, "raw": resp.text, "final_payload": params}

# -------- Flask Webhook --------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return {"status": "Server läuft", "routes": ["/signal (POST)"]}

@app.route("/signal", methods=["POST"])
def signal():
    data = request.get_json(silent=True)
    if not data or data == {}:
        if request.form:
            data = request.form.to_dict()
        else:
            data = request.args.to_dict()

    logging.info(f"[WEBHOOK] Signal empfangen: {data}")

    try:
        symbol = norm_symbol(str(data.get("symbol", "BTC-USDT")))
        side = str(data.get("side", "SELL")).upper()
        size = float(data.get("size", ORDER_SIZE_USDT))
        lev = int(data.get("leverage", ORDER_LEVERAGE))
    except Exception as e:
        return jsonify({"status": "error", "message": f"Payload parse error: {e}", "received": data}), 400

    try:
        result = bingx_place_order(symbol, side, size, lev)
        return jsonify({
            "status": "ok",
            "received_signal": {"symbol": symbol, "side": side, "size": size, "leverage": lev},
            "bingx_payload": result.get("final_payload"),   # <-- zeigt die gesendete Payload
            "bingx_result": result                          # <-- zeigt die Antwort von BingX
        }), 200
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "received_signal": {"symbol": symbol, "side": side, "size": size, "leverage": lev}
        }), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Render setzt $PORT automatisch
    app.run(host="0.0.0.0", port=port)
