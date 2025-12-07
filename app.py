import time
import hmac
import hashlib
import asyncio
import logging
import aiohttp
from flask import Flask, request

# -------- Deine festen Daten --------
BINGX_API_KEY = "XeyESAWMvOPHPPlteKkem15yGzEPvHauxKj5LORpjrvOipxPza5DiWkGSMJGhWZyIKp0ZNQwhN17R3aon1RA"
BINGX_SECRET = "EKHC1rgjFzQVBO9noJa1CHaeoh9vJqv78EXg76aqozvejJbTknkaVr2G3fJyUcBZs1rCoSRA5vMQ6gZYmIg"

# -------- Trading Parameter --------
ORDER_SIZE_USDT = 10       # feste Ordergröße
ORDER_LEVERAGE = 10        # fester Leverage
TP_PERCENT = 2.0           # Take Profit in Prozent
SL_PERCENT = 100.0         # Stop Loss in Prozent

BINGX_BASE = "https://api-swap-rest.bingx.com"
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# -------- Utils --------
def ts_ms():
    return int(time.time() * 1000)

def sign_query(query_str: str, secret: str):
    return hmac.new(secret.encode(), query_str.encode(), hashlib.sha256).hexdigest()

# -------- BingX API --------
async def bingx_get_price(session: aiohttp.ClientSession, symbol: str) -> float | None:
    url = f"{BINGX_BASE}/api/v1/market/getLatestPrice"
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
    url = f"{BINGX_BASE}/api/v1/order"
    price = await bingx_get_price(session, symbol)
    if not price or price <= 0:
        raise RuntimeError("Preis nicht verfügbar")

    qty = round((notional_usdt / price), 6)
    if qty <= 0:
        raise RuntimeError("Qty <= 0")

    # Market Order
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "positionSide": "SHORT" if side == "SELL" else "LONG",
        "quantity": qty,
        "reduceOnly": "false",
        "leverage": str(leverage),
        "timestamp": str(ts_ms()),
    }
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature = sign_query(query, BINGX_SECRET)
    headers = {"X-BX-APIKEY": BINGX_API_KEY, "Content-Type": "application/json"}
    payload = {**params, "signature": signature}

    async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
        txt = await resp.text()
        logging.info(f"Market Order Response: {txt}")

    # TP / SL Orders analog wie in deinem alten Code
    # ...

# -------- Flask Webhook --------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return {"status": "Server läuft"}

@app.route("/signal", methods=["POST"])
def signal():
    data = request.json
    logging.info(f"[WEBHOOK] Signal empfangen: {data}")

    symbol = data.get("symbol", "BTC-USDT")
    side = data.get("side", "SELL")
    size = float(data.get("size", ORDER_SIZE_USDT))
    lev = int(data.get("leverage", ORDER_LEVERAGE))

    asyncio.run(_handle_signal(symbol, side, size, lev))
    return {"status": "ok"}

async def _handle_signal(symbol, side, size, lev):
    async with aiohttp.ClientSession() as session:
        await bingx_place_order(session, symbol, side, size, lev,
                                tp_percent=TP_PERCENT, sl_percent=SL_PERCENT)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
