# -------- V 4.2: BINGX FUTURES - Auto TP, SL, BE Monitor, NUR SHORT MIT RSI < 80 FILTER --------

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
TP_PERCENT, SL_PERCENT, BE_PERCENT = 3.0, 1.5, 0.5
# Feste Trade-Richtung für dieses Skript
TRADE_SIDE = "SHORT"

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

# ---------------- INDICATORS ----------------

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------- POSITION ACTIONS ----------------

def get_active_position(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        for p in r.get("data", []):
             if float(p.get("positionAmt", 0)) != 0: return p 
        return None
    except: return None

def close_position_market(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions?{qs}&signature={sig}"
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print(f"[EXIT] {symbol} Markt-Close ausgeführt.")

def set_tp_sl(symbol, qty, tp_price, sl_price):
    # Exit side for SHORT is BUY
    exit_side = "BUY"
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": exit_side, "positionSide": TRADE_SIDE,
            "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        qs = urllib.parse.urlencode(sorted(params.items()))
        sig = sign_bingx(params)
        url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}"
        requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- MONITORING ----------------

def monitor_trade(symbol, entry, tp, sl, be_trigger):
    be_active = False
    while get_active_position(symbol):
        curr = get_price_bingx(symbol)
        if not curr: time.sleep(2); continue

        # Prüfe, ob BE-Trigger erreicht wurde (Profit bei SHORT: Preis < Trigger)
        if not be_active and curr <= be_trigger:
            be_active = True
            print(f"[BE STATUS] {symbol} Profit erreicht. BE-Schutz ist jetzt scharf.")

        # Wenn BE scharf ist: Schließen sobald Kurs zurück am Entry ist (Preis > Entry)
        if be_active and curr >= entry:
            print(f"[BE EXIT] {symbol} Kurs zurück am Entry. Schließe Position.")
            close_position_market(symbol)
            break
        
        # Lokaler Sicherheits-Check auf TP oder SL (TP Hit: Preis < TP, SL Hit: Preis > SL)
        tp_hit = curr <= tp
        sl_hit = curr >= sl

        if tp_hit or sl_hit:
            reason = "TP" if tp_hit else "SL"
            print(f"[LOCAL EXIT] {symbol} {reason} erreicht. Schließe...")
            close_position_market(symbol)
            break

        time.sleep(3)
    print(f"[MONITOR] Ende für {symbol}")

# ---------------- EXECUTION LOGIC (NUR SHORT) ----------------

def execute_trade_bingx(symbol):
    
    # RSI Check (Einstiegs-Trigger) im 1m TF
    ohlcv_asset = get_ohlcv(symbol, RSI_TIMEFRAME)
    if not ohlcv_asset: return
    rsi = calc_rsi([float(c["close"]) for c in ohlcv_asset])
    
    # Filterbedingung: RSI muss unter 80 sein
    if rsi >= 80:
        print(f"[RSI FILTER] Kein Einstiegssignal für {symbol}. RSI={rsi:.1f} >= 80")
        return

    # Order Ausführung
    price = get_price_bingx(symbol)
    if not price: return
    
    trade_size_usdt, leverage = 10, 10
    qty = round(trade_size_usdt / price, 6)
    order_side = "SELL"
    
    print(f"[ENTRY] {TRADE_SIDE} {symbol} @ {price} | RSI: {rsi:.1f}")

    ts = str(int(time.time() * 1000))
    entry_p = {"symbol": symbol, "side": order_side, "positionSide": TRADE_SIDE, "type": "MARKET", "quantity": str(qty), "leverage": str(leverage), "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(entry_p.items()))
    sig = sign_bingx(entry_p)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{qs}&signature={sig}", headers={"X-BX-APIKEY": API_KEY})

    time.sleep(2)
    
    # Kalkulationen TP/SL/BE für SHORT
    tp = price * (1 - TP_PERCENT / 100)
    sl = price * (1 + SL_PERCENT / 100)
    be_trigger = price * (1 - BE_PERCENT / 100)
    
    set_tp_sl(symbol, qty, tp, sl)
    threading.Thread(target=monitor_trade, args=(symbol, price, tp, sl, be_trigger)).start()

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET": return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"status": "ignored"}), 200
    symbol = f"{currency}-USDT"
    
    if not get_active_position(symbol):
        threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()
        return jsonify({"status": "started", "symbol": symbol}), 200
    return jsonify({"status": "active"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
