# -------- V 3.0: BINGX FUTURES ONLY - SHORT ONLY + RSI FILTER + PRECISE TP/SL GOOGLE --------

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

# --- Flask ohne Access Logs starten ---
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# active_monitors wird nicht mehr benötigt, da TP/SL bei BingX liegen

# --- RSI TIMEFRAME (wählbar: "1m", "5m", "15m") ---
RSI_TIMEFRAME = "1m"

# ---------------- SIGNING ----------------

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ---------------- PRICE ----------------

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except:
        return None

# ---------------- OHLCV + RSI ----------------

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except:
        return []

def calc_rsi(closes, period=14):
    # ... (RSI Logik bleibt gleich) ...
    if len(closes) < period + 1:
        return 50

    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    avg_gain = sum(gains) / period if gains else 0.00001
    avg_loss = sum(losses) / period if losses else 0.00001

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------- POSITION CHECK ----------------

def is_pos_open_bingx(symbol):
    # ... (Position Check Logik bleibt gleich) ...
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

# ---------------- CLOSE (nicht mehr benötigt für TP/SL) ----------------

def close_bingx(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions",
        data=params,
        headers={"X-BX-APIKEY": API_KEY}
    )

# ---------------- PRECISE TP/SL SETTING (NEU) ----------------

def set_tp_sl(symbol, tp_price, sl_price, position_side="SHORT"):
    ts = str(int(time.time() * 1000))
    # Nutzt eine Trailing Stop Order-Funktion, die auch feste TP/SL Preise annimmt
    params = {
        "symbol": symbol,
        "type": "TAKE_PROFIT_MARKET", # Bei Trigger wird Marktorder ausgeführt
        "positionSide": position_side,
        "stopPrice": str(tp_price),
        "timestamp": ts
    }
    params["signature"] = sign_bingx(params)
    # Senden der TP Order
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=params, headers={"X-BX-APIKEY": API_KEY})

    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol,
        "type": "STOP_LOSS_MARKET", # Bei Trigger wird Marktorder ausgeführt
        "positionSide": position_side,
        "stopPrice": str(sl_price),
        "timestamp": ts
    }
    params["signature"] = sign_bingx(params)
    # Senden der SL Order
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=params, headers={"X-BX-APIKEY": API_KEY})
    print(f"[ORDERS GESETZT] TP @ {tp_price:.5f} | SL @ {sl_price:.5f} direkt bei BingX.")


# ---------------- SHORT ORDER ----------------

def execute_trade_bingx(symbol):

    # --- RSI CHECK ---
    # ... (RSI Logik bleibt gleich) ...
    ohlcv = get_ohlcv(symbol, RSI_TIMEFRAME, 100)
    if not ohlcv:
        return

    closes = [float(c["close"]) for c in ohlcv]
    rsi = calc_rsi(closes)

    if rsi < 80:
        print(f"[RSI BLOCK] {symbol} RSI={rsi:.1f} ({RSI_TIMEFRAME}) < 80 → Kein SHORT")
        return

    # --- Preis laden ---
    price = get_price_bingx(symbol)
    if price is None:
        return

    print(f"[ORDER] SHORT {symbol} | Entry={price} | RSI={rsi:.1f} ({RSI_TIMEFRAME})")

    # --- Einstellungen ---
    trade_size_usdt = 10
    leverage = 10

    tp_percent = 0.9  
    sl_percent = 0.8  
    be_percent = 0.4  

    qty = round(trade_size_usdt / price, 6)

    # --- Entry Order Senden ---
    params = {
        "leverage": str(leverage),
        "positionSide": "SHORT",
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

    # --- TP/SL Preise berechnen (SHORT!) ---
    entry = price
    tp = entry * (1 - tp_percent / 100)
    sl = entry * (1 + sl_percent / 100)
    be_trigger_price = entry * (1 - be_percent / 100) 

    # --- TP/SL an BingX senden (Präzise Ausführung) ---
    set_tp_sl(symbol, tp, sl, position_side="SHORT")

    # --- BE Monitoring lokal starten ---
    threading.Thread(target=monitor_position_be, args=(symbol, entry, be_trigger_price)).start()


# ---------------- BREAK-EVEN MONITOR (Lokal) ----------------

# Dieser Monitor wartet nur darauf, dass der BE getriggert wird
# und ersetzt dann den SL bei BingX.

def monitor_position_be(symbol, entry, be_trigger_price):
    
    # BE-Level: Entry minus 0.05% (um Trading-Gebühren zu decken)
    be_level = entry * (1 - 0.05 / 100) 
    
    print(f"[BE-MONITOR-START] {symbol} | Entry={entry} | BE-Trigger={be_trigger_price:.5f}")

    try:
        while True:
            curr = get_price_bingx(symbol)
            if curr is None:
                time.sleep(2) # Weniger Polling
                continue

            # Break-Even Logik (bei Short: Kurs <= Trigger)
            if curr <= be_trigger_price:
                # Hier müssen wir den SL bei BingX löschen und den neuen setzen
                cancel_all_orders(symbol)
                # Neuen SL setzen (BE Preis). TP bleibt aktiv.
                set_tp_sl(symbol, tp_price=entry * (1 - tp_percent / 100), sl_price=be_level, position_side="SHORT")
                print(f"[BE-AKTIVIERT] {symbol}: SL auf Entry (incl. Fees) verschoben: {be_level:.5f}")
                break # Monitor kann beendet werden, BingX verwaltet nun TP/SL/BE

            time.sleep(2) # Weniger Polling

    finally:
        # Hier wird active_monitors nicht mehr benötigt
        print(f"[BE-MONITOR ENDE] {symbol}")

# ---------------- ORDER CANCELLATION (NEU) ----------------

def cancel_all_orders(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/cancelAllOrders",
        data=params,
        headers={"X-BX-APIKEY": API_KEY}
    )
    print(f"[ORDERS GELÖSCHT] Alle offenen Orders für {symbol} storniert.")


# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["GET", "POST"])
def handle_alert():
    # ... (Webhook Logik bleibt gleich, active_monitors Check entfernt) ...

    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "ok"}), 200

    currency = str(data.get("currency", "")).upper()

    if not currency:
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"
    print(f"[SIGNAL] {symbol}")

    # Prüfen, ob Position bereits offen ist (active_monitors wird nicht mehr benötigt)
    if is_pos_open_bingx(symbol):
        return jsonify({"status": "already_active"}), 200

    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()

    return jsonify({"status": "short_started", "symbol": symbol}), 200

# ---------------- ANTI-SLEEP ----------------

def keep_alive():
    while True:
        try:
            # Polling Intervall erhöht, um Server zu entlasten
            requests.get("https://flask-webhook-bot-1.onrender.com/testorder")
        except:
            pass
        time.sleep(300)

threading.Thread(target=keep_alive, daemon=True).start()

# ---------------- START ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
