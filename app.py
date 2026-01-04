# -------- V 3.9: BINGX FUTURES - RSI ONLY --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify
import logging

# --- API Konfiguration ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- Strategie Settings ---
RSI_TIMEFRAME = "1m"
RSI_PERIOD = 14
LEVERAGE = 10
TRADE_SIZE = 10
TP_PERCENT, SL_PERCENT = 3.5, 3.5

# --- Break-Even Settings (nur LONG) ---
BE_ACTIVATION_PERCENT = 1.0
active_be_positions = {}

# ---------------- SIGNING & HELPERS ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except:
        return None

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except:
        return []

def get_open_positions():
    ts = str(int(time.time() * 1000))
    params = {"timestamp": ts}
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    try:
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return r.get("data", [])
    except:
        return []

# ---------------- INDIKATOREN ----------------

def calc_rsi(closes, period):
    if len(closes) < period + 1:
        return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------- POSITION ACTIONS ----------------

def close_position_market(symbol, side):
    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol,
        "side": "BUY" if side == "SHORT" else "SELL",
        "positionSide": side,
        "type": "MARKET",
        "closePosition": "true",
        "timestamp": ts
    }
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?" \
          f"{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print(f"[CLOSE] {symbol} {side} geschlossen.")

def set_tp_sl(symbol, qty, tp_price, sl_price, side):
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type": o_type,
            "quantity": str(qty),
            "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": ts
        }
        requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
            f"{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}",
            headers={"X-BX-APIKEY": API_KEY}
        )

    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- BREAK-EVEN MONITOR (nur LONG) ----------------

def monitor_break_even():
    while True:
        try:
            positions = get_open_positions()
            active_long_symbols = [
                p['symbol'] for p in positions
                if p.get('positionSide') == 'LONG' and float(p.get('positionAmt', 0)) > 0
            ]

            for sym in list(active_be_positions.keys()):
                if sym not in active_long_symbols:
                    del active_be_positions[sym]

            for pos in positions:
                if pos.get('positionSide') == 'LONG' and float(pos.get('positionAmt', 0)) > 0:
                    symbol = pos['symbol']
                    entry_price = float(pos['avgPrice'])
                    current_price = get_price_bingx(symbol)
                    if not current_price:
                        continue

                    profit_pct = (current_price - entry_price) / entry_price * 100

                    if profit_pct >= BE_ACTIVATION_PERCENT and symbol not in active_be_positions:
                        active_be_positions[symbol] = True
                        print(f"[BE] Aktiviert für {symbol}")

                    if active_be_positions.get(symbol) and current_price <= entry_price:
                        close_position_market(symbol, "LONG")
                        del active_be_positions[symbol]
        except:
            pass

        time.sleep(10)

# ---------------- TRADE LOGIK (RSI ONLY) ----------------

def execute_trade_bingx(symbol):
    positions = get_open_positions()
    if any(p['symbol'] == symbol and float(p.get('positionAmt', 0)) != 0 for p in positions):
        print(f"[SKIP] {symbol} bereits offen.")
        return

    ohlcv_rsi = get_ohlcv(symbol, RSI_TIMEFRAME, limit=RSI_PERIOD + 1)
    if not ohlcv_rsi:
        #print(f"[ERROR] Keine RSI-Daten für {symbol}")
        return

    closes = [float(c["close"]) for c in ohlcv_rsi]
    rsi = calc_rsi(closes, RSI_PERIOD)
    current_price = get_price_bingx(symbol)

    if not current_price:
        print(f"[ERROR] Kein Preis für {symbol}")
        return

    qty = round(TRADE_SIZE / current_price, 6)

    # -------- LONG ENTRY (RSI <= 25) --------
    if rsi <= 25:
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "BUY", "positionSide": "LONG",
            "type": "MARKET", "quantity": str(qty),
            "leverage": str(LEVERAGE), "timestamp": ts
        }
        requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
            f"{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}",
            headers={"X-BX-APIKEY": API_KEY}
        )
        time.sleep(1)
        set_tp_sl(symbol, qty, current_price * 1.05, current_price * 0.985, "LONG")
        print(f"[LONG ENTRY] {symbol} RSI={rsi:.2f}")

    # -------- SHORT ENTRY (RSI >= 80) --------
    elif rsi >= 80:
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "SELL", "positionSide": "SHORT",
            "type": "MARKET", "quantity": str(qty),
            "leverage": str(LEVERAGE), "timestamp": ts
        }
        requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
            f"{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}",
            headers={"X-BX-APIKEY": API_KEY}
        )
        time.sleep(1)
        set_tp_sl(symbol, qty, current_price * 0.95, current_price * 1.015, "SHORT")
        print(f"[SHORT ENTRY] {symbol} RSI={rsi:.2f}")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()

    if not currency:
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"
    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()

    return jsonify({"status": "processing", "symbol": symbol}), 200

# ---------------- START ----------------

if __name__ == "__main__":
    threading.Thread(target=monitor_break_even, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
