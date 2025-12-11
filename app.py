# -------- VER 1.8: Auto Orders/TP/SL/Monitoring/Cooldown/BE + Pump-Filter + Debug --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# ---------------- SIGNING ----------------

def sign_params(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ---------------- PRICE + POSITIONS ----------------

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
    print("CloseAll response:", resp.json())
    return resp.json()

# ---------------- DYNAMIC ROUND ----------------

def dynamic_round(price, value):
    if price > 1000:
        decimals = 2
    elif price > 1:
        decimals = 4
    else:
        decimals = 6
    return round(value, decimals)

# ---------------- OHLCV + RSI ----------------

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

# ---------------- PUMP FILTER LOGIC ----------------

def check_short_conditions(symbol, pump_percent, logger):
    logger.info(f"[CHECK] SHORT-Analyse für {symbol} bei Pump {pump_percent}% gestartet")

    ohlcv = get_ohlcv(symbol, "1m", 10)
    if len(ohlcv) < 6:
        return False, "Nicht genug OHLCV"

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
    rsi_overbought = rsi > 80

    current_price = closes[-1]
    previous_price = closes[-2]
    real_pump = (current_price - previous_price) / previous_price * 100

    if real_pump < pump_percent:
        return False, f"Pump nicht bestätigt ({real_pump:.2f}% < {pump_percent}%)"

    logger.info(
        f"[DATA] Pump={real_pump:.2f}% | VolSpike={vol_spike} | Wick={wick_reversal} | "
        f"Momentum={momentum_falling} | RSI={rsi:.1f}"
    )

    if not vol_spike:
        return False, "Kein Volumen-Spike"
    if not wick_reversal:
        return False, "Kein Wick-Reversal"
    if not momentum_falling:
        return False, "Momentum noch stark"
    if not rsi_overbought:
        return False, f"RSI {rsi:.1f} nicht überkauft"

    return True, f"SHORT bestätigt: Pump {pump_percent}%, Volumen, Wick, Momentum, RSI"

# ---------------- POSITION MONITOR ----------------

active_monitors = {}

def monitor_position(symbol, entry_price, tp_price, sl_price, interval=1):
    print(f"Monitoring SHORT {symbol}... TP={tp_price}, SL={sl_price}")
    active_monitors[symbol] = True
    try:
        trailing_percent = 0.025
        be_set = False

        while True:
            current = get_price(symbol)
            print(f"Current {symbol} price:", current)

            if not be_set and current <= entry_price * (1 - trailing_percent):
                sl_price = entry_price
                be_set = True
                print(f"BE aktiviert für SHORT {symbol}: SL={sl_price}")

            if current <= tp_price or current >= sl_price:
                print(f"Target reached, closing SHORT {symbol}")
                close_all_positions(symbol)
                break

            time.sleep(interval)
    finally:
        active_monitors[symbol] = False

# ---------------- COOLDOWN ----------------

cooldowns = {}
COOLDOWN_SECONDS = 2 * 60 * 60

# ---------------- HEALTH CHECK ----------------

@app.route("/", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "ok", "message": "Webhook erreichbar"}), 200

# ---------------- DEBUG ROUTE ----------------

@app.route("/debug", methods=["GET"])
def debug_logs():
    try:
        with open("render.log", "r") as f:
            lines = f.readlines()[-200:]
        return "<br>".join(lines), 200
    except:
        return "Keine Logs gefunden", 200

# ---------------- MAIN WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def handle_alert():
    try:
        data = request.get_json(force=True, silent=True) or {}

        if not data.get("currency"):
            return jsonify({"status": "ok", "message": "Webhook erreichbar"}), 200

        currency = str(data.get("currency", "")).upper()
        symbol = f"{currency}-USDT"

        pump_percent = float(data.get("percent", 0))

        if pump_percent < 5:
            app.logger.info(f"[IGNORED] Pump {pump_percent}% < 5% — kein Trade")
            return jsonify({"status": "ignored", "reason": "Pump unter 5%"}), 200

        ok, reason = check_short_conditions(symbol, pump_percent, app.logger)
        app.logger.info(f"[RESULT] {reason}")

        if not ok:
            return jsonify({"status": "ignored", "reason": reason}), 200

        now = time.time()
        last_exec = cooldowns.get(symbol, 0)
        if now - last_exec < COOLDOWN_SECONDS:
            return jsonify({
                "status": "cooldown",
                "message": f"{symbol} ist noch im Cooldown.",
                "remaining_seconds": int(COOLDOWN_SECONDS - (now - last_exec))
            }), 200

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

        cooldowns[symbol] = now

        return jsonify({
            "status": "ok",
            "alert_received": data,
            "symbol": symbol,
            "side": side,
            "entry_price": price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "entry_response": entry_resp.json(),
            "positions_response": get_positions()
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

# ---------------- RUN ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
