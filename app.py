import os
import time
import hmac
import hashlib
import asyncio
import logging
import aiohttp
from flask import Flask, request, jsonify

# -------- API Keys aus Environment Variablen --------
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_SECRET")

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
        raise RuntimeError("BINGX_API_KEY/BINGX_SECRET fehlen in Environment Variables")

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
        logging.info(f"Market Order Response: {txt}")
        return {"status_code": resp.status, "response": txt}

# -------- Flask Webhook --------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return {"status": "Server läuft"}

@app.route("/signal", methods=["POST"])
def signal():
    data = request.get_json(silent=True) or {}
    logging.info(f"[WEBHOOK] Signal empfangen: {data}")

    symbol = norm_symbol(data.get("symbol", "BTC-USDT"))
    side = data.get("side", "SELL").upper()
    size = float(data.get("size", ORDER_SIZE_USDT))
    lev = int(data.get("leverage", ORDER_LEVERAGE))

    try:
        result = asyncio.run(_handle_signal(symbol, side, size, lev))
        return jsonify({"status": "ok", "bingx_result": result}), 200
    except Exception as e:
        logging.error(f"Order Fehler: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

async def _handle_signal(symbol, side, size, lev):
    async with aiohttp.ClientSession() as session:
        return await bingx_place_order(session, symbol, side, size, lev,
                                       tp_percent=TP_PERCENT, sl_percent=SL_PERCENT)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
