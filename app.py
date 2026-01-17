import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
import sys
import logging
from pathlib import Path
from flask import Flask, jsonify

# --- CONFIG / ENV ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# --- LOGGING SETUP ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("tradingbot")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s %(levelname)s [TID=%(thread)d] %(name)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
if os.getenv("LOG_FILE"):
    fh = logging.FileHandler(os.getenv("LOG_FILE"))
    fh.setFormatter(formatter)
    logger.addHandler(fh)

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# --- DEFAULT SETTINGS ---
LEVERAGE = int(os.getenv("LEVERAGE", 20))
TRADE_SIZE = float(os.getenv("TRADE_SIZE", 20))
TP_PERCENT = float(os.getenv("TP_PERCENT", 1))
SL_PERCENT = float(os.getenv("SL_PERCENT", 40))

# --- DCA SETTINGS ---
DCA_INTERVAL = int(os.getenv("DCA_INTERVAL", 5))
DCA_COUNT = int(os.getenv("DCA_COUNT", 5))
DCA_DEVIATION_PERCENT = float(os.getenv("DCA_DEVIATION_PERCENT", 100))
DCA_VOLUME_MULTIPLIER = float(os.getenv("DCA_VOLUME_MULTIPLIER", 2))

active_dca = {}
dca_lock = threading.Lock()
last_dca_heartbeat = time.time()

# --- RSI / WATCHLIST SETTINGS ---
WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "BTC-USDT,ETH-USDT").split(",") if s.strip()]
RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
RSI_INTERVAL = os.getenv("RSI_INTERVAL", "1m")   # Kline-Intervall
RSI_CHECK_INTERVAL = int(os.getenv("RSI_CHECK_INTERVAL", 60))  # Sekunden

rsi_state = {}
rsi_lock = threading.Lock()

# --- Safety / runtime flags ---
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
MIN_RSI_DELTA = float(os.getenv("MIN_RSI_DELTA", "0.0"))

# --- Resolved symbol persistence ---
RESOLVED_MAP_FILE = os.getenv("RESOLVED_MAP_FILE", "resolved_symbols.json")
_resolved_map_lock = threading.Lock()

def load_resolved_map():
    p = Path(RESOLVED_MAP_FILE)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("load_resolved_map failed: %s", e)
        return {}

def save_resolved_map(m):
    try:
        with open(RESOLVED_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("save_resolved_map failed: %s", e, exc_info=True)

resolved_map = load_resolved_map()

# --- SIGNATURE ---
def sign_bingx(params):
    if not params:
        query_string = ""
    else:
        items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
        query_string = urllib.parse.urlencode(items)
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    logger.debug("sign_bingx query=%s signature=%s", query_string, sig)
    return sig

# --- API REQUEST ---
def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)
    timeout = (5, 10)

    logger.debug("API request start method=%s endpoint=%s params=%s", method, endpoint, params)
    if method == "GET":
        try:
            params_for_sign = dict(params)
            signature = sign_bingx(params_for_sign)
            params_for_sign["signature"] = signature
            query = urllib.parse.urlencode(params_for_sign)
            full_url = f"{url}?{query}"
            logger.debug("API GET url=%s", full_url)
            response = requests.get(full_url, headers=headers, timeout=timeout)
            logger.debug("HTTP GET %s -> status=%s body=%s", full_url, response.status_code, response.text[:4000])
            response.raise_for_status()
            j = response.json()
            logger.debug("API GET response keys=%s", list(j.keys()) if isinstance(j, dict) else type(j))
            return j
        except Exception as e:
            logger.error("API ERROR GET %s %s", endpoint, e, exc_info=True)
            return None

    if method == "POST":
        try:
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))
            query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params_for_sign.items()))
            signature = sign_bingx(params_for_sign)
            full_url = f"{url}?{query}&signature={signature}"
            logger.debug("API POST url=%s", full_url)
            response = requests.post(full_url, headers=headers, timeout=timeout)
            logger.debug("HTTP POST %s -> status=%s body=%s", full_url, response.status_code, response.text[:4000])
            response.raise_for_status()
            j = response.json()
            logger.debug("API POST response keys=%s", list(j.keys()) if isinstance(j, dict) else type(j))
            return j
        except Exception as e:
            logger.error("API ERROR POST %s %s", endpoint, e, exc_info=True)
            return None

# --- HELPERS ---
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if not r:
        logger.debug("get_price no response for %s", symbol)
        return None
    try:
        price = float(r["data"]["price"])
        logger.debug("get_price %s = %s", symbol, price)
        return price
    except Exception as e:
        logger.error("get_price parse error for %s: %s", symbol, e, exc_info=True)
        return None

def get_positions():
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/user/positions", {"timestamp": ts})
    if not r:
        logger.debug("get_positions no response")
        return []
    data = r.get("data", [])
    logger.debug("get_positions count=%d", len(data) if isinstance(data, list) else 0)
    return data

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    exists = bool(r and "data" in r and "price" in r["data"])
    logger.debug("symbol_exists %s -> %s", symbol, exists)
    return exists

def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(leverage), "timestamp": ts}
    if position_side:
        params["positionSide"] = position_side
    if side:
        params["side"] = side
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    ok = bool(r)
    logger.debug("set_leverage_for_symbol %s leverage=%s ok=%s", symbol, leverage, ok)
    return ok

# --- TP/SL ---
def reset_tp_sl(symbol, position_side=None):
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
    orders = r.get("data", {}).get("orders", []) if r else []
    logger.debug("reset_tp_sl %s found_orders=%d", symbol, len(orders))
    for order in orders:
        pos_side = order.get("positionSide") or order.get("position")
        if position_side and pos_side != position_side:
            continue
        oid = order.get("orderId")
        if not oid:
            continue
        api_request("POST", "/openApi/swap/v2/trade/cancelOrder",
                    {"orderId": oid, "symbol": symbol, "timestamp": str(int(time.time() * 1000))})
        logger.info("reset_tp_sl cancelled order %s for %s", oid, symbol)

def set_tp_sl(symbol, desired_side=None, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    pos = None
    for _ in range(8):
        positions = get_positions()
        pos = next((p for p in positions
                    if p["symbol"] == symbol
                    and float(p.get("positionAmt", 0)) != 0
                    and (desired_side is None or p.get("positionSide") == desired_side)), None)
        if pos:
            break
        time.sleep(1)

    if not pos:
        logger.warning("set_tp_sl position not found for %s", symbol)
        return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])
    logger.debug("set_tp_sl %s side=%s entry=%s", symbol, side, entry)

    for _ in range(10):
        time.sleep(0.8)
        new_pos = next((p for p in get_positions()
                        if p["symbol"] == symbol and p.get("positionSide") == side), None)
        if new_pos and abs(float(new_pos["avgPrice"]) - entry) > 0.0001:
            entry = float(new_pos["avgPrice"])
            logger.debug("set_tp_sl updated entry to %s", entry)
            break

    tp = entry * (1 + tp_percent / 100) if side == "LONG" else entry * (1 - tp_percent / 100)
    sl = entry * (1 - sl_percent / 100) if side == "LONG" else entry * (1 + sl_percent / 100)

    reset_tp_sl(symbol, side)

    def place(price, otype):
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type": otype,
            "stopPrice": f"{price:.6f}",
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": str(int(time.time() * 1000))
        })
        logger.info("set_tp_sl placed %s for %s at %s", otype, symbol, price)

    place(tp, "TAKE_PROFIT_MARKET")
    place(sl, "STOP_MARKET")

# ============================================================
#   DCA ENGINE
# ============================================================
def update_entry(symbol, side):
    positions = get_positions()
    pos = next((p for p in positions
                if p["symbol"] == symbol and p["positionSide"] == side), None)
    if pos:
        return float(pos["avgPrice"])
    return None

def calculate_dca_qty(base_trade_size, executed, current_price):
    multiplier = DCA_VOLUME_MULTIPLIER ** (executed + 1)
    qty = round((base_trade_size * multiplier) / current_price, 6)
    logger.debug("calculate_dca_qty base=%s executed=%s price=%s qty=%s", base_trade_size, executed, current_price, qty)
    return qty

def should_trigger_dca(side, current, entry_static, deviation_percent):
    if side == "LONG":
        return current <= entry_static * (1 - deviation_percent / 100)
    else:
        return current >= entry_static * (1 + deviation_percent / 100)

def monitor_dca():
    global last_dca_heartbeat
    while True:
        last_dca_heartbeat = time.time()
        try:
            positions = get_positions()
            for pos in positions:
                symbol = pos["symbol"]
                side = pos["positionSide"]
                amt = float(pos["positionAmt"])
                if amt == 0:
                    continue

                current_price = get_price(symbol)
                if not current_price:
                    continue

                with dca_lock:
                    if symbol not in active_dca:
                        base_value = abs(amt) * float(pos["avgPrice"])
                        active_dca[symbol] = {
                            "side": side,
                            "entry_static": float(pos["avgPrice"]),
                            "entry_dynamic": float(pos["avgPrice"]),
                            "executed": 0,
                            "base_trade_size": base_value,
                            "tp_percent": TP_PERCENT,
                            "sl_percent": SL_PERCENT
                        }
                        logger.debug("monitor_dca initialized active_dca for %s: %s", symbol, active_dca[symbol])

                    d = active_dca[symbol]

                if d["executed"] >= DCA_COUNT:
                    continue

                if not should_trigger_dca(side, current_price, d["entry_static"], DCA_DEVIATION_PERCENT):
                    continue

                qty = calculate_dca_qty(
                    d["base_trade_size"],
                    d["executed"],
                    current_price
                )

                api_request("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": symbol,
                    "side": "BUY" if side == "LONG" else "SELL",
                    "positionSide": side,
                    "type": "MARKET",
                    "quantity": str(qty),
                    "timestamp": str(int(time.time() * 1000))
                })
                logger.info("monitor_dca placed DCA order %s qty=%s", symbol, qty)

                with dca_lock:
                    d["executed"] += 1
                    new_entry = update_entry(symbol, side)
                    if new_entry:
                        d["entry_dynamic"] = new_entry

                reset_tp_sl(symbol, side)
                set_tp_sl(symbol, side, d["tp_percent"], d["sl_percent"])

        except Exception as e:
            logger.error("DCA ERROR %s", e, exc_info=True)

        time.sleep(DCA_INTERVAL)

# ============================================================
#   TP/SL WATCHER
# ============================================================
def tp_sl_watcher():
    while True:
        try:
            positions = get_positions()
            logger.debug("[TP/SL WATCHER] Prüfe Positionen... ID=%s", threading.get_ident())
            for pos in positions:
                symbol = pos["symbol"]
                side = pos["positionSide"]
                amt = float(pos["positionAmt"])
                if amt == 0:
                    continue

                ts = str(int(time.time() * 1000))
                r = api_request("GET", "/openApi/swap/v2/trade/openOrders",
                                {"symbol": symbol, "timestamp": ts})
                orders = r.get("data", {}).get("orders", []) if r else []

                has_tp = any(o.get("type") == "TAKE_PROFIT_MARKET" and o.get("positionSide") == side for o in orders)
                has_sl = any(o.get("type") == "STOP_MARKET" and o.get("positionSide") == side for o in orders)

                logger.debug("TP/SL WATCHER %s %s TP=%s SL=%s", symbol, side, has_tp, has_sl)

                if not has_tp or not has_sl:
                    logger.info("TP/SL WATCHER Setze TP/SL neu für %s (%s)", symbol, side)
                    reset_tp_sl(symbol, side)

        except Exception as e:
            logger.error("TP/SL WATCHER ERROR %s", e, exc_info=True)

        time.sleep(10)

# ============================================================
#   execute_trade
# ============================================================
def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    logger.info("execute_trade called symbol=%s direction=%s leverage=%s trade_size=%s", symbol, direction, leverage, trade_size)
    if not symbol_exists(symbol):
        logger.warning("execute_trade abort symbol does not exist %s", symbol)
        return

    positions = get_positions()
    if any(p["symbol"] == symbol and p.get("positionSide") == direction and float(p.get("positionAmt", 0)) != 0 for p in positions):
        logger.info("execute_trade skip position already open %s %s", symbol, direction)
        return

    price = get_price(symbol)
    if not price:
        logger.warning("execute_trade abort no price for %s", symbol)
        return

    logger.debug("execute_trade price=%s", price)
    if not set_leverage_for_symbol(symbol, leverage, direction, "BUY" if direction == "LONG" else "SELL"):
        logger.error("execute_trade leverage error for %s", symbol)
        return

    qty = round(trade_size / price, 6)
    logger.info("Placing market order %s qty=%s", symbol, qty)

    if DRY_RUN:
        logger.info("DRY_RUN enabled — skipping real order placement for %s", symbol)
    else:
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction,
            "type": "MARKET",
            "quantity": str(qty),
            "timestamp": str(int(time.time() * 1000))
        })

    with dca_lock:
        active_dca[symbol] = {
            "side": direction,
            "entry_static": price,
            "entry_dynamic": price,
            "executed": 0,
            "base_trade_size": trade_size,
            "tp_percent": tp_percent,
            "sl_percent": sl_percent
        }
        logger.debug("execute_trade active_dca set for %s: %s", symbol, active_dca[symbol])

    time.sleep(2)
    reset_tp_sl(symbol, direction)
    set_tp_sl(symbol, direction, tp_percent, sl_percent)

# ============================================================
#   KLINES / HYBRID RSI
# ============================================================
def get_klines_raw_try_endpoint(symbol, endpoint, interval="1m", limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
    logger.debug("get_klines try endpoint=%s symbol=%s", endpoint, symbol)
    r = api_request("GET", endpoint, params)
    logger.debug("get_klines raw response for %s @ %s: %s", symbol, endpoint, json.dumps(r, default=str)[:4000])
    if not r:
        return None
    code = r.get("code")
    if code == 100400:
        logger.info("Endpoint %s not available for %s (code=100400).", endpoint, symbol)
        return {"error_code": 100400, "response": r}
    data = r.get("data")
    if not data:
        return None
    closes = None
    if isinstance(data, list) and len(data) and isinstance(data[0], dict) and "close" in data[0]:
        try:
            closes = [float(item["close"]) for item in data]
        except Exception as e:
            logger.error("get