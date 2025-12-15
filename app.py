# -------- V 3.0: BINGX FUTURES + EXAKTER TP/SL/BE + MARK PRICE --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# --- API Konfiguration BingX ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# Globaler Status für aktive Überwachungen
active_monitors = {}

# Cache für Binance Trend
btc_cache = {
    "trend": "NEUTRAL",
    "timestamp": 0
}

# --- HILFSFUNKTIONEN ---

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    """Holt den stabilen Mark-Preis von BingX."""
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/markPrice"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["markPrice"])
    except Exception as e:
        print(f"[ERROR PREIS] {symbol}: {e}")
        return None

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions",
                         params=params,
                         headers={"X-BX-APIKEY": API_KEY},
                         timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except:
        return True

def close_bingx(symbol):
    print(f"[BINGX] Schließe Position für {symbol}")
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions",
                  data=params,
                  headers={"X-BX-APIKEY": API_KEY})

# --- BTC TREND MIT CACHE ---

def get_btc_hourly_trend():
    now = time.time()

    if now - btc_cache["timestamp"] < 60:
        return btc_cache["trend"]

    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "limit": 2
    }

    try:
        r = requests.get(url, params=params, timeout=10)

        if r.status_code != 200:
            print(f"[ERROR TREND] Binance Status {r.status_code}: {r.text}")
            return btc_cache["trend"]

        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return btc_cache["trend"]

        last_hour = data[0]
        open_price = float(last_hour[1])
        close_price = float(last_hour[4])

        if close_price > open_price:
            trend = "LONG"
        elif close_price < open_price:
            trend = "SHORT"
        else:
            trend = "NEUTRAL"

        btc_cache["trend"] = trend
        btc_cache["timestamp"] = now

        print(f"[TREND] BTC 1H: {trend}")
        return trend

    except Exception as e:
        print(f"[ERROR TREND] Binance Fehler: {e}")
        return btc_cache["trend"]

# --- ORDER & MONITORING LOGIK ---

def execute_trade_bingx(symbol, side):
    print(f"[BINGX] Starte {side} Order für {symbol}")
    price = get_price_bingx(symbol)
    if not price:
        return

    trade_size_usdt = 20
    leverage = 20

    if side == "LONG":
        tp_percent = 0.5
        sl_percent = 0.5
    else:
        tp_percent = 0.5
        sl_percent = 0.5

    qty = round(trade_size_usdt / price, 6)

    params = {
        "leverage": str(leverage),
        "positionSide": side,
        "quantity": str(qty),
        "side": "BUY" if side == "LONG" else "SELL",
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)

    res = requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/order",
        data=params,
        headers={"X-BX-APIKEY": API_KEY},
        timeout=10
    ).json()

    entry_price = float(res.get("data", {}).get("avgPrice", price))

    if side == "LONG":
        tp_price = entry_price * (1 + tp_percent / 100)
        sl_price = entry_price * (1 - sl_percent / 100)
    else:
        tp_price = entry_price * (1 - tp_percent / 100)
        sl_price = entry_price * (1 + sl_percent / 100)

    threading.Thread(target=monitor_position,
                     args=(symbol, entry_price, tp_price, sl_price, side)).start()

def monitor_position(symbol, entry, tp, sl, side):
    key = f"BINGX_{symbol}"
    active_monitors[key] = True
    print(f"[MONITOR] START {symbol} ({side}) | Entry: {entry:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")

    try:
        spread = entry * 0.0005
        be_trigger_long = entry * 1.02 + spread
        be_trigger_short = entry * 0.98 - spread
        be_set = False

        while True:
            curr = get_price_bingx(symbol)
            if not curr:
                time.sleep(1)
                continue

            if not be_set:
                if side == "LONG" and curr >= be_trigger_long:
                    sl = entry
                    be_set = True
                    print(f"[BE] {symbol} aktiviert! SL auf Entry gesetzt.")
                elif side == "SHORT" and curr <= be_trigger_short:
                    sl = entry
                    be_set = True
                    print(f"[BE] {symbol} aktiviert! SL auf Entry gesetzt.")

            if (side == "LONG" and (curr >= tp or curr <= sl)) or \
               (side == "SHORT" and (curr <= tp or curr >= sl)):

                reason = "TP" if ((side == "LONG" and curr >= tp) or
                                  (side == "SHORT" and curr <= tp)) else "SL/BE"

                print(f"[EXIT] {symbol} Triggered durch {reason} bei Preis: {curr:.4f}")
                close_bingx(symbol)
                break

            time.sleep(1)

    except Exception as e:
        print(f"[ERROR MONITOR] {symbol}: {e}")

    finally:
        active_monitors[key] = False
        print(f"[MONITOR] END {symbol}")

# ---------------- HEALTH CHECK ----------------

@app.route("/", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "ok", "message": "Webhook erreichbar"}), 200

@app.route("/debug", methods=["GET"])
def debug_logs():
    return "Bitte Render Dashboard → Logs öffnen.", 200

# --- FLASK WEBHOOK HANDLER ---

@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency:
        return jsonify({"error": "no currency"}), 400

    symbol = f"{currency}-USDT"
    print(f"\n--- SIGNAL EMPFANGEN: {symbol} ---")

    if is_pos_open_bingx(symbol) or active_monitors.get(f"BINGX_{symbol}"):
        return jsonify({"status": "already_active", "symbol": symbol}), 200

    btc_trend = get_btc_hourly_trend()

    if btc_trend == "LONG":
        threading.Thread(target=execute_trade_bingx, args=(symbol, "LONG")).start()
        return jsonify({"status": "order_started_long", "symbol": symbol, "btc_trend": btc_trend}), 200

    elif btc_trend == "SHORT":
        threading.Thread(target=execute_trade_bingx, args=(symbol, "SHORT")).start()
        return jsonify({"status": "order_started_short", "symbol": symbol, "btc_trend": btc_trend}), 200

    else:
        return jsonify({"status": "trend_neutral_no_order", "symbol": symbol, "btc_trend": btc_trend}), 200

# --- APP START ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
