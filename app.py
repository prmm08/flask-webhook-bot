import time
import hmac
import hashlib
import asyncio
import logging
import aiohttp
from flask import Flask, request, jsonify

# -------- API Keys direkt im Code --------
API_KEY = "XeyESAWMvOPHPPlteKkem15yGzEPvHauxKj5LORpjrvOipxPza5DiWkGSMJGhWZyIKp0ZNQwhN17R3aon1RA"
API_SECRET = "EKHC1rgjFzQVBO9noJa1CHaeoh9vJqv78EXg76aqozvejJbTknkaVr2G3fJyUcBZs1rCoSRA5vMQ6gZYmIg"

# -------- Trading Parameter --------
ORDER_SIZE_USDT = 10
ORDER_LEVERAGE = 5
TP_PERCENT = 2.0
SL_PERCENT = 100.0

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

# -------- BingX API --------
async def bingx_get_price(session: aiohttp.ClientSession, symbol: str) -> float | None:
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    params = {"symbol": symbol}
    async with session.get(url, params=params, timeout=10) as r:
        if r.status != 200:
            return None
        data = await r.json()
        try:
            return float(data["data"]["price"])
        except:
            return None

async def bingx_place_order(session: aiohttp.ClientSession, symbol: str, side: str,
                            notional_usdt: float, leverage: int,
                            tp_percent: float = None, sl_percent: float = None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("API Keys fehlen im Code!")

    url = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
    price = await bingx_get_price(session, symbol)
    if not price or price <= 0:
        raise RuntimeError("Preis nicht verfügbar")

    qty = round((notional_usdt / price), 6)
    if qty <= 0:
        raise RuntimeError("Qty <= 0")

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "positionSide": "SHORT" if side.upper() == "SELL" else "LONG",
        "quantity": str(qty),
        "leverage": str(leverage),
        "timestamp": str(ts_ms())
    }

    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature = sign_query(query, API_SECRET)
    params["signature"] = signature

    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}

    async with session.post(url, data=params, headers=headers, timeout=10) as resp:
        txt = await resp.text()
        logging.info(f"[BINGX] Market Order Response: {txt}")
        try:
            data = await resp.json()
            return {"status_code": resp.status, "data": data, "raw": txt}
        except Exception:
            return {"status_code": resp.status, "raw": txt}

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
        logging.error(f"[WEBHOOK] Payload parse error: {e}")
        return jsonify({"status": "error", "message": f"Payload parse error: {e}", "received": data}), 400

    try:
        result = asyncio.run(_handle_signal(symbol, side, size, lev))
        return jsonify({
            "status": "ok",
            "received": {"symbol": symbol, "side": side, "size": size, "leverage": lev},
            "bingx_result": result
        }), 200
    except Exception as e:
        logging.error(f"[WEBHOOK] Order Fehler: {e}")
        return jsonify({
            "status": "error",
            "message": str(e),
            "received": {"symbol": symbol, "side": side, "size": size, "leverage": lev}
        }), 400

async def _handle_signal(symbol, side, size, lev):
    async with aiohttp.ClientSession() as session:
        return await bingx_place_order(session, symbol, side, size, lev,
                                       tp_percent=TP_PERCENT, sl_percent=SL_PERCENT)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
