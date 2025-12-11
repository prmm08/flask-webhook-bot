# -------- VER 2.0: BingX Pump-Bot + KuCoin Futures Bot in einem Flask-Service --------
#
# BingX Bot (Pump + 45 Minuten Watcher, TP/SL/BE, Monitoring, Cooldown)
# KuCoin Futures Bot (Spread/Orderbook Check, Market Short, TP/SL/BE, Monitoring, Cooldown)
# Weiterleitung vom BingX Webhook zur KuCoin Route
# Kompakte Logs + robustere KuCoin-API-Checks + dynamische Orderbook-Tiefe

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import logging
import base64
import json
from flask import Flask, request, jsonify

# ---------------- FLASK + LOGGING SETUP ----------------

app = Flask(__name__)

app.logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
app.logger.addHandler(handler)

# ---------------- BINGX CONFIG ----------------

API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# ---------------- KUCOIN FUTURES CONFIG ----------------

KUCOIN_FUTURES_BASE = "https://api-futures.kucoin.com"

KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET")            # plain text secret
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE")    # plain text passphrase

# ---------------- COMMON HELPERS ----------------

def sign_params(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def kucoin_futures_sign(method, endpoint, query_string, body=""):
    now = int(time.time() * 1000)
    pre_sign = str(now) + method + endpoint + query_string + body
    signature = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode(), pre_sign.encode(), hashlib.sha256).digest()
    ).decode()

    passphrase = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest()
    ).decode()

    headers = {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }
    return headers

def dynamic_round(price, value):
    if price > 1000:
        decimals = 2
    elif price > 1:
        decimals = 4
    else:
        decimals = 6
    return round(value, decimals)

# ---------------- BINGX PRICE + POSITIONS ----------------

def get_price(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    return float(r.json()["data"]["price"])

def get_positions():
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {"timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_params(params)
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    return resp.json()

def close_all_positions(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_params(params)
    resp = requests.post(url, data=params, headers=headers, timeout=10)
    app.logger.info(f"[CLOSE] {resp.json()}")
    return resp.json()

# ---------------- BINGX OHLCV + RSI ----------------

def get_ohlcv(symbol, interval="1m", limit=10):
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    return r.json().get("data", [])

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.00001
    avg_loss = sum(losses) / period if losses else 0.00001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------- BINGX FILTER OHNE PUMP (für Watcher) ----------------

def check_reversal_conditions(symbol, logger):
    ohlcv = get_ohlcv(symbol, "1m", 100)
    if len(ohlcv) < 6:
        return False, "NO (Nicht genug OHLCV)"

    closes = [float(c["close"]) for c in ohlcv]
    volumes = [float(c["volume"]) for c in ohlcv]

    last = ohlcv[-1]
    open_p = float(last["open"])
    close_p = float(last["close"])
    high_p = float(last["high"])
    wick = high_p - max(open_p, close_p)
    body = abs(close_p - open_p)

    avg_vol = sum(volumes[-6:-1]) / 5
    vol_spike = volumes[-1] > avg_vol * 2
    wick_reversal = wick > body * 1.3

    p1 = get_price(symbol)
    time.sleep(0.4)
    p2 = get_price(symbol)
    time.sleep(0.4)
    p3 = get_price(symbol)
    momentum_falling = p3 < p2 < p1

    rsi = calc_rsi(closes)

    logger.info(
        f"[CHECK] {symbol} | Vol={vol_spike} | Wick={wick_reversal} | Mom={momentum_falling} | RSI={rsi:.1f}"
    )

    if not vol_spike:
        return False, "NO (Volumen fehlt)"
    if not wick_reversal:
        return False, "NO (Wick fehlt)"
    if not momentum_falling:
        return False, "NO (Momentum nicht gedreht)"
    if rsi <= 80:
        return False, f"NO (RSI {rsi:.1f} nicht überkauft)"

    return True, "YES"

# ---------------- BINGX 45-MINUTEN WATCHER ----------------

def pump_watcher(symbol, pump_percent):
    app.logger.info(f"[WATCHER] gestartet für {symbol} (45 Minuten)")

    start = time.time()
    max_duration = 45 * 60  # 45 Minuten

    while time.time() - start < max_duration:
        ok, reason = check_reversal_conditions(symbol, app.logger)

        if ok:
            app.logger.info(f"[WATCHER RESULT] Bedingungen erfüllt → SHORT")
            trigger_short(symbol)
            return

        time.sleep(60)

    app.logger.info(f"[WATCHER END] Keine Bedingungen erfüllt nach 45 Minuten")

# ---------------- BINGX SHORT AUSLÖSEN + MONITORING ----------------

active_monitors = {}
cooldowns = {}
COOLDOWN_SECONDS = 2 * 60 * 60

def trigger_short(symbol):
    side = "SELL"
    size = 100
    leverage = 20
    tp_percent = 5
    sl_percent = 2

    price = get_price(symbol)
    qty = round(size / price, 6)

    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"

    entry_params = {
        "leverage": str(leverage),
        "positionSide": "SHORT",
        "quantity": str(qty),
        "side": side,
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    entry_params["signature"] = sign_params(entry_params)
    entry_resp = requests.post(url_order, data=entry_params, headers=headers, timeout=10)

    tp_price = dynamic_round(price, price * (1 - tp_percent / 100))
    sl_price = dynamic_round(price, price * (1 + sl_percent / 100))

    if not active_monitors.get(symbol, False):
        threading.Thread(
            target=monitor_position,
            args=(symbol, price, tp_price, sl_price)
        ).start()

    cooldowns[symbol] = time.time()

    app.logger.info(f"[SHORT] {symbol} eröffnet bei {price} TP={tp_price} SL={sl_price}")
    return entry_resp.json(), price, tp_price, sl_price

def monitor_position(symbol, entry_price, tp_price, sl_price, interval=1):
    app.logger.info(f"[MONITOR] {symbol} gestartet")
    active_monitors[symbol] = True
    try:
        trailing_percent = 0.025
        be_set = False

        while True:
            current = get_price(symbol)
            app.logger.info(f"[PRICE] {symbol} = {current}")

            if not be_set and current <= entry_price * (1 - trailing_percent):
                sl_price = entry_price
                be_set = True
                app.logger.info(f"[BE] Break-Even aktiviert für {symbol}")

            if current <= tp_price or current >= sl_price:
                app.logger.info(f"[EXIT] {symbol} TP/SL erreicht")
                close_all_positions(symbol)
                break

            time.sleep(interval)
    finally:
        active_monitors[symbol] = False

# ---------------- KUCOIN FUTURES HELPERS ----------------

def kucoin_symbol_from_currency(currency):
    mapping = {
        "BTC": "XBTUSDTM",
        "XBT": "XBTUSDTM",
        "ETH": "ETHUSDTM",
        "SOL": "SOLUSDTM",
        "XRP": "XRPUSDTM",
        "DOGE": "DOGEUSDTM",
        "ADA": "ADAUSDTM",

        # ✅ wichtig für dich:
        "JCT": "JCTUSDTM",
    }
    return mapping.get(currency, f"{currency}USDTM")


def kucoin_futures_get_mark_price(symbol):
    # 1) Versuch: offizieller Mark Price
    endpoint = "/api/v1/mark-price"
    query = f"symbol={symbol}"
    url = KUCOIN_FUTURES_BASE + endpoint + "?" + query
    r = requests.get(url, timeout=10)
    data = r.json()

    # Wenn Mark Price existiert → nutzen
    if "data" in data and data["data"] and "value" in data["data"]:
        return float(data["data"]["value"])

    # 2) Fallback: Orderbook holen
    app.logger.warning(f"[KUCOIN WARNING] Kein Mark Price für {symbol}, nutze Mid-Price")
    ob = kucoin_futures_get_orderbook(symbol)
    if not ob:
        raise Exception("KuCoin Mark Price API returned no data AND Orderbook empty")

    bids = ob.get("bids", [])
    asks = ob.get("asks", [])

    if not bids or not asks:
        raise Exception("KuCoin Mark Price API returned no data AND no bids/asks")

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])

    # Mid-Price als Ersatz
    return (best_bid + best_ask) / 2


def kucoin_futures_get_orderbook(symbol):
    endpoint = "/api/v1/level2/depth20"
    query = f"symbol={symbol}"
    url = KUCOIN_FUTURES_BASE + endpoint + "?" + query
    r = requests.get(url, timeout=10)
    data = r.json()
    if "data" not in data:
        app.logger.error(f"[KUCOIN ERROR] Orderbook Response: {data}")
        return None
    return data["data"]

# ---------------- KUCOIN FUTURES SPREAD-/ORDERBOOK-STRATEGIE ----------------

def kucoin_check_conditions(symbol, logger):
    ob = kucoin_futures_get_orderbook(symbol)
    if not ob:
        return False, "NO (Orderbook leer)"

    bids = ob.get("bids", [])
    asks = ob.get("asks", [])

    if len(bids) == 0 or len(asks) == 0:
        return False, "NO (Keine Bids/Asks)"

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])

    spread = (best_ask - best_bid) / best_bid * 100

    bid_depth = sum(float(b[1]) for b in bids[:5])
    ask_depth = sum(float(a[1]) for a in asks[:5])

    # ✅ dynamische Mindesttiefe: 0.1% vom Preis, aber mindestens 5 Kontrakte
    mark_price = kucoin_futures_get_mark_price(symbol)
    min_depth = max(5, mark_price * 0.001)

    logger.info(
        f"[KUCOIN CHECK] {symbol} | Spread={spread:.3f}% | "
        f"BidDepth={bid_depth:.2f} | AskDepth={ask_depth:.2f} | MinDepth={min_depth:.2f}"
    )

    if spread > 0.15:
        return False, f"NO (Spread {spread:.3f}% > 0.15%)"

    if bid_depth < min_depth:
        return False, f"NO (Bid-Tiefe {bid_depth:.2f} < {min_depth:.2f})"

    if ask_depth < min_depth:
        return False, f"NO (Ask-Tiefe {ask_depth:.2f} < {min_depth:.2f})"

    return True, "YES"


# ---------------- KUCOIN FUTURES ORDERS + MONITORING ----------------

kucoin_cooldowns = {}
KUCOIN_COOLDOWN_SECONDS = 2 * 60 * 60
kucoin_active_monitors = {}

def kucoin_futures_place_short(symbol, logger):
    side = "sell"
    leverage = 20
    size_usdt = 100
    tp_percent = 5
    sl_percent = 2

    mark_price = kucoin_futures_get_mark_price(symbol)
    qty = int(size_usdt / mark_price)
    if qty < 1:
       qty = 1


    endpoint = "/api/v1/orders"
    url = KUCOIN_FUTURES_BASE + endpoint

    body_dict = {
        "symbol": symbol,
        "side": side,
        "leverage": str(leverage),
        "type": "market",
        "size": str(qty)
    }

    body = json.dumps(body_dict)
    headers = kucoin_futures_sign("POST", endpoint, "", body)

    r = requests.post(url, headers=headers, data=body, timeout=10)
    resp = r.json()
    logger.info(f"[KUCOIN ORDER] {resp}")

    entry_price = mark_price
    tp_price = round(entry_price * (1 - tp_percent / 100), 4)
    sl_price = round(entry_price * (1 + sl_percent / 100), 4)

    if not kucoin_active_monitors.get(symbol, False):
        threading.Thread(
            target=kucoin_monitor_position,
            args=(symbol, entry_price, tp_price, sl_price)
        ).start()

    kucoin_cooldowns[symbol] = time.time()

    logger.info(f"[KUCOIN SHORT] {symbol} eröffnet bei {entry_price}, TP={tp_price}, SL={sl_price}")
    return resp, entry_price, tp_price, sl_price

def kucoin_futures_close_all(symbol, logger):
    endpoint = "/api/v1/position/close-position"
    url = KUCOIN_FUTURES_BASE + endpoint

    body_dict = {"symbol": symbol}
    body = json.dumps(body_dict)
    headers = kucoin_futures_sign("POST", endpoint, "", body)

    r = requests.post(url, headers=headers, data=body, timeout=10)
    resp = r.json()
    logger.info(f"[KUCOIN CLOSE] {resp}")
    return resp

def kucoin_monitor_position(symbol, entry_price, tp_price, sl_price, interval=1):
    app.logger.info(f"[KUCOIN MONITOR] {symbol} gestartet")
    kucoin_active_monitors[symbol] = True
    try:
        trailing_percent = 0.025
        be_set = False

        while True:
            current = kucoin_futures_get_mark_price(symbol)
            app.logger.info(f"[KUCOIN PRICE] {symbol} = {current}")

            if not be_set and current <= entry_price * (1 - trailing_percent):
                sl_price = entry_price
                be_set = True
                app.logger.info(f"[KUCOIN BE] Break-Even aktiviert für {symbol}")

            if current <= tp_price or current >= sl_price:
                app.logger.info(f"[KUCOIN EXIT] {symbol} TP/SL erreicht")
                kucoin_futures_close_all(symbol, app.logger)
                break

            time.sleep(interval)
    finally:
        kucoin_active_monitors[symbol] = False

# ---------------- HEALTH + DEBUG ----------------

@app.route("/", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "ok", "message": "Webhook erreichbar"}), 200

@app.route("/debug", methods=["GET"])
def debug_logs():
    return "Bitte Render Dashboard → Logs öffnen.", 200

# ---------------- BINGX MAIN WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def handle_alert():
    try:
        raw = request.data
        app.logger.info(f"[RAW] {raw}")

        try:
            data = request.get_json(force=True)
            app.logger.info(f"[JSON] {data}")
        except Exception as e:
            app.logger.error(f"[JSON ERROR] {e}")
            return jsonify({"status": "error", "message": "JSON Fehler"}), 400

        # Weiterleitung an KuCoin-Route im selben Service
        try:
            requests.post("https://flask-webhook-bot-1.onrender.com/kucoin", json=data, timeout=3)
            app.logger.info("[FORWARD] Signal an KuCoin weitergeleitet")
        except Exception as e:
            app.logger.error(f"[FORWARD ERROR] {e}")

        if not data or "currency" not in data or "percent" not in data:
            app.logger.warning("[IGNORED] Ungültiges JSON")
            return jsonify({"status": "ignored", "reason": "Ungültiges JSON"}), 200

        currency = str(data["currency"]).upper()
        symbol = f"{currency}-USDT"
        pump_percent = float(data["percent"])

        app.logger.info(f"[RECEIVED] {symbol} Pump={pump_percent}%")

        if pump_percent < 5:
            return jsonify({"status": "ignored", "reason": "Pump < 5%"}), 200

        # Cooldown prüfen
        now = time.time()
        last_exec = cooldowns.get(symbol, 0)
        if now - last_exec < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - (now - last_exec))
            app.logger.info(f"[COOLDOWN] {symbol} {remaining}s")
            return jsonify({"status": "cooldown", "remaining_seconds": remaining}), 200

        # Sofortige Prüfung der Reversal-Bedingungen
        ok, reason = check_reversal_conditions(symbol, app.logger)
        app.logger.info(f"[RESULT] {reason}")

        if ok:
            entry_resp, entry_price, tp_price, sl_price = trigger_short(symbol)
            return jsonify({
                "status": "ok",
                "reason": "Sofort ausgelöst",
                "symbol": symbol,
                "entry_price": entry_price,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "entry_response": entry_resp,
                "positions_response": get_positions()
            }), 200

        # Watcher starten (45 Minuten)
        threading.Thread(target=pump_watcher, args=(symbol, pump_percent)).start()

        return jsonify({"status": "watching", "reason": "Watcher gestartet (45 Minuten)"}), 200

    except Exception as e:
        app.logger.error(f"[ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

# ---------------- KUCOIN FUTURES WEBHOOK ----------------

@app.route("/kucoin", methods=["POST"])
def kucoin_handler():
    try:
        raw = request.data
        app.logger.info(f"[KUCOIN RAW] {raw}")

        try:
            data = request.get_json(force=True)
            app.logger.info(f"[KUCOIN JSON] {data}")
        except Exception as e:
            app.logger.error(f"[KUCOIN JSON ERROR] {e}")
            return jsonify({"status": "error", "message": "JSON Fehler"}), 400

        if not data or "currency" not in data or "percent" not in data:
            app.logger.warning("[KUCOIN IGNORED] Ungültiges JSON")
            return jsonify({"status": "ignored", "reason": "Ungültiges JSON"}), 200

        currency = str(data["currency"]).upper()
        fut_symbol = kucoin_symbol_from_currency(currency)
        pump_percent = float(data["percent"])

        app.logger.info(f"[KUCOIN RECEIVED] {fut_symbol} Pump={pump_percent}%")

        # Cooldown prüfen
        now = time.time()
        last_exec = kucoin_cooldowns.get(fut_symbol, 0)
        if now - last_exec < KUCOIN_COOLDOWN_SECONDS:
            remaining = int(KUCOIN_COOLDOWN_SECONDS - (now - last_exec))
            app.logger.info(f"[KUCOIN COOLDOWN] {fut_symbol} {remaining}s")
            return jsonify({
                "status": "cooldown",
                "exchange": "kucoin_futures",
                "symbol": fut_symbol,
                "remaining_seconds": remaining
            }), 200

        # Spread + Orderbook-Strategie
        ok, reason = kucoin_check_conditions(fut_symbol, app.logger)
        app.logger.info(f"[KUCOIN RESULT] {reason}")

        if not ok:
            return jsonify({
                "status": "ignored",
                "exchange": "kucoin_futures",
                "symbol": fut_symbol,
                "reason": reason
            }), 200

        # Market-Short + TP/SL/BE + Monitoring
        order_resp, entry_price, tp_price, sl_price = kucoin_futures_place_short(fut_symbol, app.logger)

        return jsonify({
            "status": "ok",
            "exchange": "kucoin_futures",
            "symbol": fut_symbol,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "order_response": order_resp
        }), 200

    except Exception as e:
        app.logger.error(f"[KUCOIN ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

# ---------------- RUN ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
