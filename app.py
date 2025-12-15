# -------- V 2.6: BINGX FUTURES ONLY - NO BTC TREND FILTER --------

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

# --- HILFSFUNKTIONEN ---

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except Exception as e:
        print(f"[ERROR PREIS] {symbol}: {e}")
        return None

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(
            f"{BINGX_BASE}/openApi/swap/v2/user/positions",
            params=params,
            headers={"X-BX-APIKEY": API_KEY},
            timeout=10
        ).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except:
        return True

def close_bingx(symbol):
    print(f"[BINGX] Schließe Position für {symbol}")
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions",
        data=params,
        headers={"X-BX-APIKEY": API_KEY}
    )

# --- ORDER & MONITORING LOGIK ---

def execute_trade_bingx(symbol):
    """Platziert IMMER einen SHORT."""
    side = "SHORT"
    print(f"[BINGX] Starte SHORT Order für {symbol}")

    price = get_price_bingx(symbol)
    if not price:
        return

    trade_size_usdt = 20
    leverage = 20

    tp_percent = 0.75
    sl_percent = 0.5

    qty = round(trade_size_usdt / price, 6)

    params = {
        "leverage": str(leverage),
        "positionSide": side,
        "quantity": str(qty),
        "side": "SELL",
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)

    requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/order",
        data=params,
        headers={"X-BX-APIKEY": API_KEY},
        timeout=10
    )

    entry_price = price
    tp_price = entry_price * (1 - tp_percent / 100)
    sl_price = entry_price * (1 + sl_percent / 100)

    threading.Thread(
        target=monitor_position,
        args=(symbol, entry_price, tp_price, sl_price, side)
    ).start()

def monitor_position(symbol, entry, tp, sl, side):
    key = f"BINGX_{symbol}"
    active_monitors[key] = True

    print(f"[MONITOR] START {symbol} SHORT | Entry: {entry:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")

    try:
        be_trigger_short = entry * 0.98
        be_set = False

        while True:
            curr = get_price_bingx(symbol)
            if not curr:
                time.sleep(1)
                continue

            # Break-Even
            if not be_set and curr <= be_trigger_short:
                sl = entry
                be_set = True
                print(f"[BE] {symbol} aktiviert! SL auf Entry gesetzt.")

            # TP oder SL
            if curr <= tp or curr >= sl:
                reason = "TP" if curr <= tp else "SL/BE"
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

# --- WEBHOOK: IMMER SHORT ---

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

    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()

    return jsonify({"status": "short_started", "symbol": symbol}), 200

# --- APP START ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
