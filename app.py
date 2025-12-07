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
        "