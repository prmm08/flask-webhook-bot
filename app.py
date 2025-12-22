# -------- V 2.9: BINGX FUTURES - SIMULTAN LONG & SHORT BEI RSI >= 70 (HEDGE MODE REQUIRED) --------

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

# Globale Settings
RSI_TIMEFRAME = "1m"
TP_PERCENT, SL_PERCENT = 1.5, 0.5 # BE_PERCENT entfernt
TRADE_SIZE = 10  # USDT (Größe PRO Position, d.h. 20 USDT total pro Signal)
LEVERAGE = 10

# ---------------- SIGNING & HELPERS ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except: return None

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except: return []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------- POSITION ACTIONS ----------------

def set_tp_sl(symbol, qty, tp_price, sl_price, side):
    """Setzt TP/SL für eine spezifische Seite (LONG/SHORT) im Hedge Mode."""
    exit_side = "SELL" if side == "LONG" else "BUY"
    
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": exit_side, "positionSide": side, # WICHTIG: positionSide spezifizieren
            "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        # Hier behandeln wir mögliche API-Fehler robuster
        try:
            r = requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}", headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
            if r.get("code") != 0:
                print(f"[ERROR] Fehler beim Setzen von {o_type} für {side}: {r.get('msg')}")
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Netzwerkfehler beim Setzen von {o_type} für {side}: {e}")
    
    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol):
    
    ohlcv_asset = get_ohlcv(symbol, RSI_TIMEFRAME)
    if not ohlcv_asset: return
    rsi = calc_rsi([float(c["close"]) for c in ohlcv_asset])
    
    # NEUE BEDINGUNG: Nur wenn RSI >= 70
    if RSI >= 70:
        price = get_price_bingx(symbol)
        if not price: return
        qty = round(TRADE_SIZE / price, 6)
        
        ts = str(int(time.time() * 1000))

        # --- LONG Trade Eröffnung ---
        side_long = "LONG"
        order_side_long = "BUY"
        tp_long = price * (1 + TP_PERCENT / 100)
        sl_long = price * (1 - SL_PERCENT / 100)
        
        print(f"[ENTRY] {side_long} {symbol} @ {price} | RSI: {rsi:.1f} (Bedingung erfüllt)")
        entry_p_long = {
            "symbol": symbol, "side": order_side_long, "positionSide": side_long, 
            "type": "MARKET", "quantity": str(qty), "leverage": str(LEVERAGE), "timestamp": ts
        }
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(entry_p_long.items()))}&signature={sign_bingx(entry_p_long)}", headers={"X-BX-APIKEY": API_KEY})
        time.sleep(1)
        set_tp_sl(symbol, qty, tp_long, sl_long, side_long)

        # --- SHORT Trade Eröffnung ---
        side_short = "SHORT"
        order_side_short = "SELL"
        tp_short = price * (1 - TP_PERCENT / 100)
        sl_short = price * (1 + SL_PERCENT / 100)

        print(f"[ENTRY] {side_short} {symbol} @ {price} | RSI: {rsi:.1f} (Bedingung erfüllt)")
        entry_p_short = {
            "symbol": symbol, "side": order_side_short, "positionSide": side_short, 
            "type": "MARKET", "quantity": str(qty), "leverage": str(LEVERAGE), "timestamp": ts
        }
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(entry_p_short.items()))}&signature={sign_bingx(entry_p_short)}", headers={"X-BX-APIKEY": API_KEY})
        time.sleep(1)
        set_tp_sl(symbol, qty, tp_short, sl_short, side_short)
        
    else:
        print(f"[RSI FILTER] Kein Signal für {symbol}. RSI={rsi:.1f} ist unter 75.")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET": return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"status": "ignored"}), 200
    symbol = f"{currency}-USDT"
    
    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()
    return jsonify({"status": "processing", "symbol": symbol}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
