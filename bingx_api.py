import time
import hmac
import hashlib
import urllib.parse
import requests
from config import API_KEY, API_SECRET, BINGX_BASE


# ---------------------------------------------------------
#   SIGNATURE
# ---------------------------------------------------------
def sign(params: dict) -> str:
    """
    BingX requires HMAC SHA256 signature over sorted query params.
    """
    items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
    query = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------
#   GENERIC REQUEST WRAPPER
# ---------------------------------------------------------
def api_request(method: str, endpoint: str, params: dict = None):
    """
    Unified request handler for BingX API.
    Deterministic, minimal, stable.
    """
    if params is None:
        params = {}

    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    timeout = (5, 10)

    try:
        if method == "GET":
            params_for_sign = dict(params)
            signature = sign(params_for_sign)
            params_for_sign["signature"] = signature
            query = urllib.parse.urlencode(params_for_sign)
            r = requests.get(f"{url}?{query}", headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()

        elif method == "POST":
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))

            query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params_for_sign.items()))
            signature = sign(params_for_sign)

            r = requests.post(f"{url}?{query}&signature={signature}",
                              headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()

    except Exception as e:
        print(f"[BINGX API ERROR] {method} {endpoint} → {e}")
        return None


# ---------------------------------------------------------
#   PRICE
# ---------------------------------------------------------
def get_price(symbol: str):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        return float(r["data"]["price"])
    except:
        return None


# ---------------------------------------------------------
#   POSITIONS
# ---------------------------------------------------------
def get_positions():
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/user/positions", {"timestamp": ts})
    if not r:
        return []
    return r.get("data", [])


# ---------------------------------------------------------
#   SYMBOL CHECK
# ---------------------------------------------------------
def symbol_exists(symbol: str) -> bool:
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return r and "data" in r and "price" in r["data"]


# ---------------------------------------------------------
#   LEVERAGE
# ---------------------------------------------------------
def set_leverage(symbol, leverage, position_side, side):
    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol,
        "leverage": str(leverage),
        "positionSide": position_side,
        "side": side,
        "timestamp": ts
    }
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    return bool(r)


# ---------------------------------------------------------
#   MARKET ORDER
# ---------------------------------------------------------
def place_market_order(symbol, side, position_side, qty):
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": "MARKET",
        "quantity": str(qty),
        "timestamp": str(int(time.time() * 1000))
    }
    return api_request("POST", "/openApi/swap/v2/trade/order", params)


# ---------------------------------------------------------
#   CLOSE POSITION (MARKET)
# ---------------------------------------------------------
def close_position_market(symbol, side):
    params = {
        "symbol": symbol,
        "side": "SELL" if side == "LONG" else "BUY",
        "positionSide": side,
        "type": "MARKET",
        "closePosition": "true",
        "timestamp": str(int(time.time() * 1000))
    }
    return api_request("POST", "/openApi/swap/v2/trade/order", params)


# ---------------------------------------------------------
#   OPEN ORDERS
# ---------------------------------------------------------
def get_open_orders(symbol):
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders",
                    {"symbol": symbol, "timestamp": ts})
    if not r:
        return []
    return r.get("data", {}).get("orders", [])


# ---------------------------------------------------------
#   CANCEL ORDER
# ---------------------------------------------------------
def cancel_order(symbol, order_id):
    params = {
        "symbol": symbol,
        "orderId": order_id,
        "timestamp": str(int(time.time() * 1000))
    }
    return api_request("POST", "/openApi/swap/v2/trade/cancelOrder", params)


# ---------------------------------------------------------
#   CANCEL ALL TP ORDERS FOR SIDE
# ---------------------------------------------------------
def cancel_all_tp(symbol, side):
    orders = get_open_orders(symbol)
    for o in orders:
        if o.get("type") == "TAKE_PROFIT_MARKET" and o.get("positionSide") == side:
            cancel_order(symbol, o["orderId"])


# ---------------------------------------------------------
#   SET TP/SL
# ---------------------------------------------------------
def set_tp_sl(symbol, side, tp_price, sl_price):
    """
    Sets TP and SL as MARKET orders with stopPrice.
    """
    def place(price, otype):
        params = {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type": otype,
            "stopPrice": f"{price:.6f}",
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": str(int(time.time() * 1000))
        }
        api_request("POST", "/openApi/swap/v2/trade/order", params)

    place(tp_price, "TAKE_PROFIT_MARKET")
    place(sl_price, "STOP_MARKET")
