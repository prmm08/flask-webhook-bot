import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json

from flask import Flask, jsonify
import logging

# --- API ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

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
WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "APR-USDT,C-USDT,COLLECT-USDT,DUSK-USDT,GRIFFAIN-USDT,ME-USDT,PIPPIN-USDT,SAND-USDT,USELESS-USDT,XPL-USDT").split(",") if s.strip()]
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
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

# --- API REQUEST ---
def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)
    timeout = (5, 10)

    if method == "GET":
        try:
            params_for_sign = dict(params)
            signature = sign_bingx(params_for_sign)
            params_for_sign["signature"] = signature
            query = urllib.parse.urlencode(params_for_sign)
            response = requests.get(f"{url}?{query}", headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print("[API ERROR GET]", e)
            return None

    if method == "POST":
        try:
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))
            query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params_for_sign.items()))
            signature = sign_bingx(params_for_sign)
            response = requests.post(f"{url}?{query}&signature={signature}", headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print("[API ERROR POST]", e)
            return None

# --- HELPERS ---
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        return float(r["data"]["price"])
    except:
        return None

def get_positions():
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/user/positions", {"timestamp": ts})
    return r.get("data", []) if r else []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return r and "data" in r and "price" in r["data"]

def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(leverage), "timestamp": ts}
    if position_side:
        params["positionSide"] = position_side
    if side:
        params["side"] = side
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    return bool(r)

# --- TP/SL ---
def reset_tp_sl(symbol, position_side=None):
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
    orders = r.get("data", {}).get("orders", []) if r else []

    for order in orders:
        pos_side = order.get("positionSide") or order.get("position")
        if position_side and pos_side != position_side:
            continue
        oid = order.get("orderId")
        if not oid:
            continue
        api_request("POST", "/openApi/swap/v2/trade/cancelOrder",
                    {"orderId": oid, "symbol": symbol, "timestamp": str(int(time.time() * 1000))})

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
        print("[ERROR] Position nicht gefunden für TP/SL")
        return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])

    # avgPrice-Update abwarten
    for _ in range(10):
        time.sleep(0.8)
        new_pos = next((p for p in get_positions()
                        if p["symbol"] == symbol and p.get("positionSide") == side), None)
        if new_pos and abs(float(new_pos["avgPrice"]) - entry) > 0.0001:
            entry = float(new_pos["avgPrice"])
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
    return round((base_trade_size * multiplier) / current_price, 6)

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

                with dca_lock:
                    d["executed"] += 1
                    new_entry = update_entry(symbol, side)
                    if new_entry:
                        d["entry_dynamic"] = new_entry

                reset_tp_sl(symbol, side)
                set_tp_sl(symbol, side, d["tp_percent"], d["sl_percent"])

        except Exception as e:
            print("[DCA ERROR]", e)

        time.sleep(DCA_INTERVAL)

# ============================================================
#   TP/SL WATCHER — setzt fehlende TP/SL neu
# ============================================================
def tp_sl_watcher():
    while True:
        try:
            positions = get_positions()

            print("[TP/SL WATCHER] Prüfe Positionen...")
            print(f"[WATCHER THREAD] ID={threading.get_ident()}")
            print("[TP/SL WATCHER]", time.strftime("%H:%M:%S"))

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

                print(f"[TP/SL WATCHER] {symbol} {side} TP={has_tp} SL={has_sl}")

                if not has_tp or not has_sl:
                    print(f"[TP/SL WATCHER] Setze TP/SL neu für {symbol} ({side})")
                    reset_tp_sl(symbol, side)
                    set_tp_sl(symbol, side)

        except Exception as e:
            print("[TP/SL WATCHER ERROR]", e)

        time.sleep(10)

# ============================================================
#   execute_trade() MIT DCA-INTEGRATION
# ============================================================
def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if not symbol_exists(symbol):
        print("[ERROR] Symbol existiert nicht:", symbol)
        return

    positions = get_positions()
    if any(p["symbol"] == symbol and p.get("positionSide") == direction and float(p["positionAmt"]) != 0 for p in positions):
        print("[SKIP] Position bereits offen:", symbol, direction)
        return

    price = get_price(symbol)
    if not price:
        print("[ERROR] Kein Preis")
        return

    if not set_leverage_for_symbol(symbol, leverage, direction, "BUY" if direction == "LONG" else "SELL"):
        print("[ERROR] Leverage Fehler")
        return

    qty = round(trade_size / price, 6)

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
        return None
    data = r.get("data")
    if not data:
        return None
    # Mögliche Formate abfangen
    if isinstance(data, list) and len(data) and isinstance(data[0], dict) and "close" in data[0]:
        return [float(item["close"]) for item in data]
    if isinstance(data, list) and len(data) and isinstance(data[0], list):
        try:
            return [float(item[4]) for item in data]
        except:
            return None
    if isinstance(data, dict):
        for key in ("klines", "candles", "items"):
            if key in data and isinstance(data[key], list):
                first = data[key][0]
                if isinstance(first, dict) and "close" in first:
                    return [float(x["close"]) for x in data[key]]
                if isinstance(first, list):
                    return [float(x[4]) for x in data[key]]
    return None

def compute_rsi(closes, period=RSI_PERIOD):
    if not closes or len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def get_latest_two_rsi(symbol, interval=RSI_INTERVAL, period=RSI_PERIOD):
    closes = get_klines(symbol, interval=interval, limit=period + 5)
    if not closes or len(closes) < period + 2:
        return None, None
    rsi_prev = compute_rsi(closes[:-1], period)
    rsi_now = compute_rsi(closes, period)
    return rsi_prev, rsi_now

def check_and_trigger_rsi():
    for symbol in WATCHLIST:
        symbol = symbol.strip()
        try:
            rsi_prev, rsi_now = get_latest_two_rsi(symbol, interval=RSI_INTERVAL, period=RSI_PERIOD)
            if rsi_prev is None or rsi_now is None:
                continue

            with rsi_lock:
                prev_state = rsi_state.get(symbol, {"last_rsi": None, "last_signal": None})
                last_rsi = prev_state.get("last_rsi")

            cross_up_30 = (rsi_prev < 30 and rsi_now >= 30)
            cross_down_70 = (rsi_prev > 70 and rsi_now <= 70)

            positions = get_positions()
            has_long = any(p["symbol"] == symbol and p.get("positionSide") == "LONG" and float(p.get("positionAmt", 0)) != 0 for p in positions)
            has_short = any(p["symbol"] == symbol and p.get("positionSide") == "SHORT" and float(p.get("positionAmt", 0)) != 0 for p in positions)

            if cross_up_30 and not has_long:
                with rsi_lock:
                    last_signal = rsi_state.get(symbol, {}).get("last_signal")
                    if last_signal != "LONG":
                        print(f"[RSI] {symbol} Cross Up 30 → Long (rsi_prev={rsi_prev:.2f}, rsi_now={rsi_now:.2f})")
                        threading.Thread(target=execute_trade, args=(symbol, "LONG", LEVERAGE, TRADE_SIZE, TP_PERCENT, SL_PERCENT)).start()
                        rsi_state[symbol] = {"last_rsi": rsi_now, "last_signal": "LONG"}
                        continue

            if cross_down_70 and not has_short:
                with rsi_lock:
                    last_signal = rsi_state.get(symbol, {}).get("last_signal")
                    if last_signal != "SHORT":
                        print(f"[RSI] {symbol} Cross Down 70 → Short (rsi_prev={rsi_prev:.2f}, rsi_now={rsi_now:.2f})")
                        threading.Thread(target=execute_trade, args=(symbol, "SHORT", LEVERAGE, TRADE_SIZE, TP_PERCENT, SL_PERCENT)).start()
                        rsi_state[symbol] = {"last_rsi": rsi_now, "last_signal": "SHORT"}
                        continue

            with rsi_lock:
                if symbol not in rsi_state:
                    rsi_state[symbol] = {"last_rsi": rsi_now, "last_signal": None}
                else:
                    rsi_state[symbol]["last_rsi"] = rsi_now

        except Exception as e:
            print(f"[RSI ERROR] {symbol} ->", e)

def rsi_monitor_loop():
    while True:
        try:
            check_and_trigger_rsi()
        except Exception as e:
            print("[RSI MONITOR ERROR]", e)
        time.sleep(RSI_CHECK_INTERVAL)

# --- KEEPALIVE (optional) ---
def keep_alive():
    url = os.getenv("SELF_PING_URL")
    if not url:
        print("[KEEPALIVE] Kein SELF_PING_URL gesetzt")
        return
    while True:
        try:
            requests.get(url, timeout=5)
        except:
            pass
        time.sleep(240)

# --- DCA WATCHDOG ---
def start_dca_thread():
    while True:
        try:
            monitor_dca()
        except Exception as e:
            print("[DCA CRASH]", e)
            time.sleep(3)

def dca_watchdog():
    global last_dca_heartbeat
    while True:
        if time.time() - last_dca_heartbeat > 15:
            print("[WATCHDOG] DCA Thread hängt → Neustart")
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
        print("FEHLER: API Keys fehlen")
    else:
        # Threads starten (daemon)
        threading.Thread(target=start_dca_thread, daemon=True).start()
        threading.Thread(target=dca_watchdog, daemon=True).start()
        threading.Thread(target=tp_sl_watcher, daemon=True).start()
        threading.Thread(target=rsi_monitor_loop, daemon=True).start()
        threading.Thread(target=keep_alive, daemon=True).start()

        # Flask nur für /ping (Health). Entfernte Webhook-Route.
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
