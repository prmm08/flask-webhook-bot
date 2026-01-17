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
WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "APRUSDT,CUSDT,COLLECTUSDT,DUSKUSDT,GRIFFAINUSDT,MEUSDT,PIPPINUSDT,SANDUSDT,USELESSUSDT,XPLUSDT").split(",") if s.strip()]
RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
RSI_INTERVAL = os.getenv("RSI_INTERVAL", "1m")   # Kline-Intervall
RSI_CHECK_INTERVAL = int(os.getenv("RSI_CHECK_INTERVAL", 60))  # Sekunden

rsi_state = {}
rsi_lock = threading.Lock()

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
            logger.debug("API GET status=%s url=%s", response.status_code, full_url)
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
            logger.debug("API POST status=%s url=%s", response.status_code, full_url)
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

    # avgPrice-Update abwarten
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
#   DCA ENGINE — STABILE VERSION
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
#   TP/SL WATCHER — setzt fehlende TP/SL neu
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
                    set_tp_sl(symbol, side)

        except Exception as e:
            logger.error("TP/SL WATCHER ERROR %s", e, exc_info=True)

        time.sleep(10)

# ============================================================
#   execute_trade() MIT DCA-INTEGRATION
# ============================================================
def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    logger.info("execute_trade called symbol=%s direction=%s leverage=%s trade_size=%s", symbol, direction, leverage, trade_size)
    if not symbol_exists(symbol):
        logger.warning("execute_trade abort symbol does not exist %s", symbol)
        return

    positions = get_positions()
    if any(p["symbol"] == symbol and p.get("positionSide") == direction and float(p.get("positionAmt")) != 0 for p in positions):
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
#   KLINES / RSI BERECHNUNG UND MONITOR
# ============================================================
def get_klines(symbol, interval="1m", limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
    r = api_request("GET", "/openApi/swap/v2/quote/kline", params)
    if not r:
        logger.warning("get_klines no response for %s", symbol)
        return None
    data = r.get("data")
    if not data:
        logger.warning("get_klines empty data for %s", symbol)
        return None
    logger.debug("get_klines raw data type=%s", type(data))

    closes = None
    # Mögliche Formate abfangen
    if isinstance(data, list) and len(data) and isinstance(data[0], dict) and "close" in data[0]:
        try:
            closes = [float(item["close"]) for item in data]
        except Exception as e:
            logger.error("get_klines parse dict-list error %s", e, exc_info=True)
    elif isinstance(data, list) and len(data) and isinstance(data[0], list):
        try:
            closes = [float(item[4]) for item in data]
        except Exception as e:
            logger.error("get_klines parse list-of-lists error %s", e, exc_info=True)
    elif isinstance(data, dict):
        for key in ("klines", "candles", "items"):
            if key in data and isinstance(data[key], list):
                first = data[key][0]
                if isinstance(first, dict) and "close" in first:
                    try:
                        closes = [float(x["close"]) for x in data[key]]
                    except Exception as e:
                        logger.error("get_klines parse dict inside data error %s", e, exc_info=True)
                elif isinstance(first, list):
                    try:
                        closes = [float(x[4]) for x in data[key]]
                    except Exception as e:
                        logger.error("get_klines parse list inside data error %s", e, exc_info=True)

    if closes:
        logger.debug("get_klines %s closes_count=%d last_close=%s", symbol, len(closes), closes[-1])
    else:
        logger.warning("get_klines could not parse closes for %s", symbol)
    return closes

def compute_rsi(closes, period=RSI_PERIOD):
    if not closes or len(closes) < period + 1:
        logger.debug("compute_rsi not enough closes len=%s period=%s", len(closes) if closes else 0, period)
        return None

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    logger.debug("compute_rsi initial avg_gain=%.6f avg_loss=%.6f", avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        logger.debug("compute_rsi avg_loss==0 returning 100")
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    logger.debug("compute_rsi result rsi=%.4f", rsi)
    return rsi

def get_latest_two_rsi(symbol, interval=RSI_INTERVAL, period=RSI_PERIOD):
    closes = get_klines(symbol, interval=interval, limit=period + 5)
    if not closes or len(closes) < period + 2:
        logger.debug("get_latest_two_rsi not enough closes for %s len=%s", symbol, len(closes) if closes else 0)
        return None, None
    rsi_prev = compute_rsi(closes[:-1], period)
    rsi_now = compute_rsi(closes, period)
    logger.debug("get_latest_two_rsi %s rsi_prev=%s rsi_now=%s", symbol, rsi_prev, rsi_now)
    return rsi_prev, rsi_now

def check_and_trigger_rsi():
    for symbol in WATCHLIST:
        symbol = symbol.strip()
        try:
            rsi_prev, rsi_now = get_latest_two_rsi(symbol, interval=RSI_INTERVAL, period=RSI_PERIOD)
            logger.debug("RSI check %s rsi_prev=%s rsi_now=%s", symbol, rsi_prev, rsi_now)
            if rsi_prev is None or rsi_now is None:
                logger.info("RSI skip %s not enough data rsi_prev=%s rsi_now=%s", symbol, rsi_prev, rsi_now)
                continue

            cross_up_30 = (rsi_prev < 30 and rsi_now >= 30)
            cross_down_70 = (rsi_prev > 70 and rsi_now <= 70)

            positions = get_positions()
            has_long = any(p["symbol"] == symbol and p.get("positionSide") == "LONG" and float(p.get("positionAmt", 0)) != 0 for p in positions)
            has_short = any(p["symbol"] == symbol and p.get("positionSide") == "SHORT" and float(p.get("positionAmt", 0)) != 0 for p in positions)

            logger.debug("RSI state %s has_long=%s has_short=%s", symbol, has_long, has_short)

            if cross_up_30:
                logger.info("RSI cross_up_30 detected for %s rsi_prev=%.2f rsi_now=%.2f", symbol, rsi_prev, rsi_now)
                if has_long:
                    logger.info("RSI not opening LONG for %s because long already open", symbol)
                else:
                    with rsi_lock:
                        last_signal = rsi_state.get(symbol, {}).get("last_signal")
                        if last_signal == "LONG":
                            logger.info("RSI not opening LONG for %s because last_signal already LONG", symbol)
                        else:
                            logger.info("Triggering LONG for %s", symbol)
                            threading.Thread(target=execute_trade, args=(symbol, "LONG", LEVERAGE, TRADE_SIZE, TP_PERCENT, SL_PERCENT)).start()
                            rsi_state[symbol] = {"last_rsi": rsi_now, "last_signal": "LONG"}
                            continue

            if cross_down_70:
                logger.info("RSI cross_down_70 detected for %s rsi_prev=%.2f rsi_now=%.2f", symbol, rsi_prev, rsi_now)
                if has_short:
                    logger.info("RSI not opening SHORT for %s because short already open", symbol)
                else:
                    with rsi_lock:
                        last_signal = rsi_state.get(symbol, {}).get("last_signal")
                        if last_signal == "SHORT":
                            logger.info("RSI not opening SHORT for %s because last_signal already SHORT", symbol)
                        else:
                            logger.info("Triggering SHORT for %s", symbol)
                            threading.Thread(target=execute_trade, args=(symbol, "SHORT", LEVERAGE, TRADE_SIZE, TP_PERCENT, SL_PERCENT)).start()
                            rsi_state[symbol] = {"last_rsi": rsi_now, "last_signal": "SHORT"}
                            continue

            with rsi_lock:
                if symbol not in rsi_state:
                    rsi_state[symbol] = {"last_rsi": rsi_now, "last_signal": None}
                else:
                    rsi_state[symbol]["last_rsi"] = rsi_now

        except Exception as e:
            logger.exception("RSI ERROR for %s", symbol)

def rsi_monitor_loop():
    while True:
        try:
            check_and_trigger_rsi()
        except Exception as e:
            logger.error("RSI MONITOR ERROR %s", e, exc_info=True)
        time.sleep(RSI_CHECK_INTERVAL)

# --- KEEPALIVE (optional) ---
def keep_alive():
    url = os.getenv("SELF_PING_URL")
    if not url:
        logger.info("[KEEPALIVE] Kein SELF_PING_URL gesetzt")
        return
    while True:
        try:
            requests.get(url, timeout=5)
            logger.debug("keep_alive pinged %s", url)
        except Exception:
            logger.warning("keep_alive ping failed", exc_info=True)
        time.sleep(240)

# --- DCA WATCHDOG ---
def start_dca_thread():
    while True:
        try:
            monitor_dca()
        except Exception as e:
            logger.error("DCA CRASH %s", e, exc_info=True)
            time.sleep(3)

def dca_watchdog():
    global last_dca_heartbeat
    while True:
        if time.time() - last_dca_heartbeat > 15:
            logger.warning("[WATCHDOG] DCA Thread hängt → Neustart")
            threading.Thread(target=start_dca_thread, daemon=True).start()
            last_dca_heartbeat = time.time()
        time.sleep(5)

# --- FLASK HEALTH ENDPOINT ---
@app.route("/ping")
def ping():
    return "pong", 200

# --- MAIN STARTUP ---
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        logger.error("FEHLER: API Keys fehlen")
    else:
        # Threads starten (daemon)
        threading.Thread(target=start_dca_thread, daemon=True).start()
        threading.Thread(target=dca_watchdog, daemon=True).start()
        threading.Thread(target=tp_sl_watcher, daemon=True).start()
        threading.Thread(target=rsi_monitor_loop, daemon=True).start()
        threading.Thread(target=keep_alive, daemon=True).start()

        logger.info("Bot gestartet. Watchlist: %s", WATCHLIST)
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
