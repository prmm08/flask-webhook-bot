# -------- VER 1.8: Auto Orders LONG/TP/SL/Monitoring/Cooldown/BE --------

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

def get_open_interest(symbol):
    """Holt Open Interest von Binance Futures."""
    binance_symbol = symbol.replace("-", "")
    url = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {"symbol": binance_symbol, "period": "5m", "limit": 1}

    try:
        r = requests.get(url, params=params, timeout=10)
        resp = r.json()
        if not isinstance(resp, list) or len(resp) == 0:
            return None
        return float(resp[0]["sumOpenInterest"])
    except:
        return None

def monitor_oi_for_long(symbol, oi_at_signal, window_minutes=15, interval=30):
    """
    Beobachtet OI nach Signal.
    Wenn OI innerhalb des Fensters STEIGT -> LONG wird ausgeführt.
    """
    print(f"[OI-Monitor] Starte OI-Überwachung für {symbol} (LONG-Trigger) für {window_minutes} Min...")
    deadline = time.time() + window_minutes * 60

    while time.time() < deadline:
        try:
            current_oi = get_open_interest(symbol)
            if current_oi and current_oi > oi_at_signal:
                print(f"[OI-Monitor] OI gestiegen -> LONG wird ausgelöst für {symbol}")
                execute_long_order(symbol)
                return True
        except Exception as e:
            print(f"[OI-Monitor] Fehler: {e}")
        time.sleep(interval)

    print(f"[OI-Monitor] OI nicht gestiegen -> Kein Long für {symbol}")
    return False

def sign_params(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_price(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    return float(r.json()["data"]["price"])

def close_all_positions(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_params(params)
    resp = requests.post(url, data=params, headers=headers, timeout=10)
    return resp.json()

def dynamic_round(price, value):
    decimals = 2 if price > 1000 else 4 if price > 1 else 6
    return round(value, decimals)

active_monitors = {}
cooldowns = {}
COOLDOWN_SECONDS = 2 * 60 * 60

def monitor_position(symbol, entry_price, tp_price, sl_price, interval=1):
    """Überwacht LONG-Position mit BE-TSL"""
    print(f"Monitoring LONG {symbol}... TP={tp_price}, SL={sl_price}")
    active_monitors[symbol] = True
    try:
        trailing_percent = 0.02 # 2% Gewinn für Break-Even
        be_set = False

        while True:
            current = get_price(symbol)
            
            # Break-Even setzen bei +2% Gewinn (Preis steigt)
            if not be_set and current >= entry_price * (1 + trailing_percent):
                sl_price = entry_price
                be_set = True
                print(f"BE aktiviert für LONG {symbol}: SL auf Entry ({sl_price})")

            # Schließen bei TP (Preis >= TP) oder SL (Preis <= SL)
            if current >= tp_price or current <= sl_price:
                print(f"Target reached, closing LONG {symbol}")
                close_all_positions(symbol)
                break

            time.sleep(interval)
    finally:
        active_monitors[symbol] = False

def execute_long_order(symbol):
    """Führt den LONG aus."""
    side = "BUY"
    size = 20 # USDT
    leverage = 20
    tp_percent = 4
    sl_percent = 2

    price = get_price(symbol)
    qty = round(size / price, 6)

    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"

    entry_params = {
        "leverage": str(leverage),
        "positionSide": "LONG",
        "quantity": str(qty),
        "side": side,
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    entry_params["signature"] = sign_params(entry_params)
    entry_resp = requests.post(url_order, data=entry_params, headers=headers, timeout=10)

    # TP liegt ÜBER Preis, SL liegt UNTER Preis
    tp_price = dynamic_round(price, price * (1 + tp_percent / 100))
    sl_price = dynamic_round(price, price * (1 - sl_percent / 100))

    if not active_monitors.get(symbol, False):
        threading.Thread(
            target=monitor_position,
            args=(symbol, price, tp_price, sl_price)
        ).start()

    cooldowns[symbol] = time.time()
    print(f"[ORDER] LONG ausgeführt für {symbol} @ {price}")
    return entry_resp.json()

@app.route("/", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "ok"}), 200

@app.route("/testorder", methods=["POST"])
def handle_alert():
    try:
        data = request.get_json(force=True, silent=True) or {}
        currency = str(data.get("currency", "")).upper()
        if not currency:
            return jsonify({"error": "No currency"}), 400
            
        symbol = f"{currency}-USDT"

        # Cooldown Check
        now = time.time()
        if now - cooldowns.get(symbol, 0) < COOLDOWN_SECONDS:
            return jsonify({"status": "cooldown"}), 200

        oi_at_signal = get_open_interest(symbol)
        if oi_at_signal is None:
            return jsonify({"status": "error", "message": "OI fail"}), 200

        threading.Thread(
            target=monitor_oi_for_long,
            args=(symbol, oi_at_signal)
        ).start()

        return jsonify({"status": "monitoring_long", "symbol": symbol}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
