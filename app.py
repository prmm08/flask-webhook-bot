# -------- V 4.0: BINGX FUTURES - MULTI-ENTRY (EMA 50/200 & RSI FILTER) --------

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
RSI_THRESHOLD = 75
EMA_TIMEFRAME = "5m"
EMA_PERIOD = 50       # Kurzer EMA (50)
EMA_PERIOD_LONG = 200 # Langer EMA (200)

LEVERAGE = 10
TRADE_SIZE = 10       
TP_PERCENT, SL_PERCENT = 1.5, 3.0

# --- Break-Even Settings ---
BE_ACTIVATION_PERCENT = 0.5
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
    except: return None

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except: return []

def get_open_positions():
    ts = str(int(time.time() * 1000))
    params = {"timestamp": ts}
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    try:
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return r.get("data", [])
    except: return []

# ---------------- INDIKATOREN ----------------

def calc_rsi(closes, period):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(closes, period):
    if not closes or len(closes) < 1: return 0
    if len(closes) < period: return closes[-1]
    alpha = 2 / (period + 1)
    ema = float(closes[0])
    for price in closes[1:]:
        ema = (float(price) * alpha) + (ema * (1 - alpha))
    return ema

# ---------------- POSITION ACTIONS ----------------

def close_position_market(symbol):
    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol, "side": "SELL", "positionSide": "LONG",
        "type": "MARKET", "closePosition": "true", "timestamp": ts
    }
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print(f"[BREAK-EVEN] {symbol} Position geschlossen.")

def set_tp_sl(symbol, qty, tp_price, sl_price):
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "SELL", "positionSide": "LONG",
            "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}", headers={"X-BX-APIKEY": API_KEY})
    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- BREAK-EVEN MONITOR ----------------

def monitor_break_even():
    while True:
        try:
            positions = get_open_positions()
            active_long_symbols = [p['symbol'] for p in positions if p.get('positionSide') == 'LONG' and float(p.get('positionAmt', 0)) > 0]
            
            for sym in list(active_be_positions.keys()):
                if sym not in active_long_symbols: del active_be_positions[sym]

            for pos in positions:
                if pos.get('positionSide') == 'LONG' and float(pos.get('positionAmt', 0)) > 0:
                    symbol = pos['symbol']
                    entry_price = float(pos['avgPrice'])
                    current_price = get_price_bingx(symbol)
                    if not current_price: continue
                    profit_pct = (current_price - entry_price) / entry_price * 100
                    
                    if profit_pct >= BE_ACTIVATION_PERCENT and symbol not in active_be_positions:
                        active_be_positions[symbol] = True
                        print(f"[BE-MODUS] Aktiviert für {symbol}")
                        
                    if active_be_positions.get(symbol) and current_price <= entry_price:
                        close_position_market(symbol)
                        if symbol in active_be_positions: del active_be_positions[symbol]
        except: pass
        time.sleep(10)

# ---------------- EXECUTION LOGIC (MULTI-ENTRY) ----------------

def execute_trade_bingx(symbol):
    # Prüfung auf offene Positionen entfernt -> Multi-Entry aktiv.

    # Daten für Indikatoren abrufen
    ohlcv_rsi = get_ohlcv(symbol, RSI_TIMEFRAME, limit=RSI_PERIOD + 1)
    ohlcv_ema_short = get_ohlcv(symbol, EMA_TIMEFRAME, limit=EMA_PERIOD)
    ohlcv_ema_long = get_ohlcv(symbol, EMA_TIMEFRAME, limit=EMA_PERIOD_LONG)
    
    if not ohlcv_rsi or not ohlcv_ema_short or not ohlcv_ema_long:
        #print(f"[ERROR] Nicht genügend Marktdaten für {symbol}")
        return
    
    # Indikatoren berechnen
    rsi = calc_rsi([float(c["close"]) for c in ohlcv_rsi], RSI_PERIOD)
    ema_short = calc_ema([float(c["close"]) for c in ohlcv_ema_short], EMA_PERIOD)
    ema_long = calc_ema([float(c["close"]) for c in ohlcv_ema_long], EMA_PERIOD_LONG)
    
    current_price = get_price_bingx(symbol)
    
    if current_price:
        # Bedingung: RSI >= Threshold UND Preis > EMA 50 UND Preis < EMA 200
        if rsi >= RSI_THRESHOLD and current_price > ema_short:# and current_price < ema_long:
            qty = round(TRADE_SIZE / current_price, 6)
            ts = str(int(time.time() * 1000))
            entry_params = {
                "symbol": symbol, "side": "BUY", "positionSide": "LONG", 
                "type": "MARKET", "quantity": str(qty), "leverage": str(LEVERAGE), "timestamp": ts
            }
            requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(entry_params.items()))}&signature={sign_bingx(entry_params)}", headers={"X-BX-APIKEY": API_KEY})
            
            time.sleep(1)
            set_tp_sl(symbol, qty, current_price*(1+TP_PERCENT/100), current_price*(1-SL_PERCENT/100))
            print(f"[ENTRY] {symbol} @ {current_price} (RSI: {rsi:.1f}, EMA50: {ema_short:.2f}, EMA200: {ema_long:.2f}).")
        else:
            print(f"[SKIP] {symbol} Filter nicht erfüllt.")

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
    threading.Thread(target=monitor_break_even, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
