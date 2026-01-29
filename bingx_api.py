# bingx_api.py
import os
import time
import hmac
import hashlib
import requests
from typing import Dict, Any

from config import BINGX_BASE, BINGX_API_KEY, BINGX_API_SECRET

BASE = BINGX_BASE.rstrip("/")

def _sign(params: Dict[str, Any], secret: str) -> str:
    items = sorted((k, str(v)) for k, v in params.items())
    qs = "&".join(f"{k}={v}" for k, v in items)
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

def place_market_order(symbol: str, side: str, quantity: float, leverage: int = 1, timeout: int = 15) -> Dict[str, Any]:
    """
    Platziert Market Order. Prüfe die API-Doku und passe Pfad/Parameter an.
    """
    if not BINGX_API_KEY or not BINGX_API_SECRET:
        raise RuntimeError("BingX credentials not set")

    path = "/api/v1/private/order"  # ANPASSEN falls nötig
    url = BASE + path
    timestamp = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "quantity": str(quantity),
        "leverage": str(leverage),
        "timestamp": str(timestamp)
    }
    params["signature"] = _sign(params, BINGX_API_SECRET)
    headers = {"X-API-KEY": BINGX_API_KEY}
    r = requests.post(url, data=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

def place_limit_order(symbol: str, side: str, price: float, quantity: float, timeout: int = 15) -> Dict[str, Any]:
    path = "/api/v1/private/order"  # ANPASSEN
    url = BASE + path
    timestamp = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "LIMIT",
        "price": str(price),
        "quantity": str(quantity),
        "timestamp": str(timestamp)
    }
    params["signature"] = _sign(params, BINGX_API_SECRET)
    headers = {"X-API-KEY": BINGX_API_KEY}
    r = requests.post(url, data=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

def place_stop_order(symbol: str, side: str, stop_price: float, quantity: float, timeout: int = 15) -> Dict[str, Any]:
    """
    Platzhalter für Stop Order (SL). API-Parameter anpassen.
    """
    path = "/api/v1/private/order"  # ANPASSEN
    url = BASE + path
    timestamp = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "STOP_MARKET",  # oder je nach API "STOP"
        "stopPrice": str(stop_price),
        "quantity": str(quantity),
        "timestamp": str(timestamp)
    }
    params["signature"] = _sign(params, BINGX_API_SECRET)
    headers = {"X-API-KEY": BINGX_API_KEY}
    r = requests.post(url, data=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

def fetch_ticker_price(symbol: str, timeout: int = 10) -> float:
    path = "/api/v1/market/ticker"  # ANPASSEN
    url = BASE + path
    r = requests.get(url, params={"symbol": symbol}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    # Anpassung je nach Response
    price = None
    if isinstance(data, dict):
        price = data.get("price") or data.get("data", {}).get("lastPrice")
    if price is None:
        raise RuntimeError("Could not fetch price")
    return float(price)
