# -------- V 3.3: BINGX FUTURES - EINHEITLICHE SIGNATUR & STABILE QUERY Microsoft to be tested->16.12.25 20.40--------

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

# Flask / Logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# Globale Settings
RSI_TIMEFRAME = "1m"
TP_PERCENT, SL_PERCENT, BE_PERCENT = 0.1, 0.5, 0.4  # TP 0.9%, SL 0.8%, BE 0.4%

# ==================== SIGNATUR / REQUEST-HELPER ====================

def sign_bingx(params: dict) -> str:
    """
    Erstellt die HMAC-SHA256 Signatur basierend auf sortierten Query-Parametern.
    """
    # Nur Keys mit nicht-None Werten
    clean_params = {k: v for k, v in params.items() if v is not None}
    query_string = urllib.parse.urlencode(sorted(clean_params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def signed_get(path: str, params: dict) -> dict:
    """
    Sendet einen signierten GET-Request an BingX Futures.
    """
    ts = str(int(time.time() * 1000))
    base_params = {"timestamp": ts}
    base_params.update(params or {})
    signature = sign_bingx(base_params)
    base_params["signature"] = signature

    url = f"{BINGX_BASE}{path}"
    headers = {"X-BX-APIKEY": API_KEY}
    r = requests.get(url, params=base_params, headers=headers, timeout=10)
    return r.json()

def signed_post(path: str, params: dict) -> dict:
    """
    Sendet einen signierten POST-Request an BingX Futures.
    Signatur wird stets über Query-String (URL) übertragen, Body bleibt leer.
    """
    ts = str(int(time.time() * 1000))
    base_params = {"timestamp": ts}
    base_params.update(params or {})
    signature = sign_bingx(base_params)
    base_params["signature"] = signature

    query_string = urllib.parse.urlencode(sorted(base_params.items()))
    url = f"{BINGX_BASE}{path}?{query_string}"
    headers = {"X-BX-APIKEY": API_KEY}
    r = requests.post(url, headers=headers, timeout=10)
    return r.json()

# ==================== PRICE & OHLCV ====================

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except Exception as e:
        print(f"[PRICE ERROR] {symbol} -> {e}")
        return None

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except Exception as e:
        print(f"[OHLCV ERROR] {symbol} -> {e}")
        return []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ==================== POSITION CHECK ====================

def is_pos_open_bingx(symbol):
    """
    Prüft, ob eine offene Position für das Symbol existiert.
    Nutzt signierten GET auf /user/positions.
    """
    try:
        r = signed_get("/openApi/swap/v2/user/positions", {"symbol": symbol})
        positions = r.get("data", [])
        return any(float(p.get("positionAmt", 0)) != 0 for p in positions)
    except Exception as e:
        print(f"[POS CHECK ERROR] {symbol} -> {e}")
        # Im Zweifel lieber True zurückgeben, um keine neue Position zu öffnen.
        return True

# ==================== TP/SL SETTING MIT RETRY ====================

def set_tp_sl(symbol, qty, tp_price, sl_price):
    tp_p = "{:.6f}".format(tp_price)
    sl_p = "{:.6f}".format(sl_price)

    def place_order(price, order_type):
        """
        Legt TP/SL-Order mit Retry an, falls Position noch nicht bereit ist.
        """
        for attempt in range(5):
            params = {
                "symbol": symbol,
                "side": "BUY",              # schließt SHORT
                "positionSide": "SHORT",
                "type": order_type,
                "quantity": str(qty),
                "stopPrice": price,
                "workingType": "MARK_PRICE",
                "closePosition": "true"
            }

            response = signed_post("/openApi/swap/v2/trade/order", params)
            msg = response.get("msg", "")
            code = response.get("code", -1)

            if code == 0:
                return response
            elif "position not exist" in msg.lower():
                print(f"[RETRY] Warte auf Position für {symbol} (Versuch {attempt+1}/5)...")
                time.sleep(1.5)
            else:
                return response
        return {"msg": "Max retries reached"}

    r_tp = place_order(tp_p, "TAKE_PROFIT_MARKET")
    r_sl = place_order(sl_p, "STOP_MARKET")

    print(f"[API RESULT] {symbol} -> TP: {r_tp.get('msg')} | SL: {r_sl.get('msg')}")

# ==================== HAUPT-TRADELOGIK ====================

def execute_trade_bingx(symbol):
    # RSI check
    ohlcv = get_ohlcv(symbol, RSI_TIMEFRAME)
    if not ohlcv:
        print(f"[NO OHLCV] {symbol} keine Daten")
        return

    closes = [float(c["close"]) for c in ohlcv]
    rsi = calc_rsi(closes)

    if rsi < 80:
        print(f"[RSI BLOCK] {symbol} RSI={rsi:.1f} < 80")
        return

    price = get_price_bingx(symbol)
    if not price:
        print(f"[NO PRICE] {symbol}")
        return

    trade_size_usdt, leverage = 10, 10
    qty = round(trade_size_usdt / price, 6)

    # 1. Entry Order (Market) – jetzt ebenfalls über signed_post
    entry_params = {
        "symbol": symbol,
        "side": "SELL",
        "positionSide": "SHORT",
        "type": "MARKET",
        "quantity": str(qty),
        "leverage": str(leverage),
    }

    r_entry = signed_post("/openApi/swap/v2/trade/order", entry_params)

    if r_entry.get("code") != 0:
        print(f"[ERROR] Entry failed for {symbol}: {r_entry.get('msg')}")
        return

    print(f"[ENTRY SUCCESS] {symbol} Short @ {price}")

    # 2. TP/SL setzen
    tp = price * (1 - TP_PERCENT / 100)
    sl = price * (1 + SL_PERCENT / 100)
    be_trigger = price * (1 - BE_PERCENT / 100)

    set_tp_sl(symbol, qty, tp, sl)

    # 3. BE-Monitor starten
    threading.Thread(target=monitor_be, args=(symbol, qty, price, tp, be_trigger), daemon=True).start()

# ==================== BE MONITOR ====================

def monitor_be(symbol, qty, entry, tp, trigger):
    """
    Überwacht den Kurs; wenn trigger erreicht ist:
    - Alle offenen Orders canceln
    - SL auf Break-Even (bzw. leicht im Profit) setzen
    """
    while is_pos_open_bingx(symbol):
        curr = get_price_bingx(symbol)
        if curr and curr <= trigger:
            # Alle offenen Orders canceln
            try:
                cancel_resp = signed_post("/openApi/swap/v2/trade/cancelAllOrders", {"symbol": symbol})
                print(f"[CANCEL ALL] {symbol} -> {cancel_resp.get('msg')}")
            except Exception as e:
                print(f"[CANCEL ERROR] {symbol} -> {e}")

            time.sleep(1)

            # SL auf nahezu Entry nachziehen
            new_sl = entry * 0.9995
            set_tp_sl(symbol, qty, tp, new_sl)
            print(f"[BE] SL auf Entry für {symbol} verschoben.")
            break

        time.sleep(3)

# ==================== WEBHOOK ====================

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency:
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"

    if not is_pos_open_bingx(symbol):
        threading.Thread(target=execute_trade_bingx, args=(symbol,), daemon=True).start()
        return jsonify({"status": "started", "symbol": symbol}), 200

    return jsonify({"status": "active"}), 200

# ==================== STARTUP ====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
