# -------- V 3.1: BINGX FUTURES - LONG ONLY (UNABHÄNGIGE TF, EMA & RSI FILTER) --------

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

# --- Strategie Settings (Unabhängige Perioden & Timeframes) ---
RSI_TIMEFRAME = "1m"
RSI_PERIOD = 14       # Unabhängige RSI Periode
RSI_THRESHOLD = 75    # Schwellenwert für Long-Entry

EMA_TIMEFRAME = "5m"  # NEU: Unabhängiger Timeframe für EMA
EMA_PERIOD = 50       # Unabhängige EMA Periode (Trendfilter)

LEVERAGE = 10
TRADE_SIZE = 10       # USDT pro Trade
TP_PERCENT, SL_PERCENT = 1.0, 1.5

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
    """Holt OHLCV Daten für einen spezifischen Timeframe."""
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
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
    # Einfache EMA Berechnung
    if len(closes) < period: return closes[-1]
    alpha = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = (price * alpha) + (ema * (1 - alpha))
    return ema

# ---------------- POSITION ACTIONS ----------------

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

# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol):
    
    # 1. Daten für RSI Timeframe abrufen
    # Benötigt mindestens Period + 1 Kerzen für die Berechnung
    ohlcv_rsi = get_ohlcv(symbol, RSI_TIMEFRAME, limit=RSI_PERIOD + 1)
    if not ohlcv_rsi: return
    closes_rsi = [float(c["close"]) for c in ohlcv_rsi]
    rsi = calc_rsi(closes_rsi, RSI_PERIOD)
    
    # 2. Daten für EMA Timeframe abrufen
    # Benötigt mindestens Period Kerzen für die Berechnung
    ohlcv_ema = get_ohlcv(symbol, EMA_TIMEFRAME, limit=EMA_PERIOD)
    if not ohlcv_ema: return
    closes_ema = [float(c["close"]) for c in ohlcv_ema]
    ema = calc_ema(closes_ema, EMA_PERIOD)
    
    # Aktuellen Marktpreis für den Entry holen
    current_price = get_price_bingx(symbol)
    if not current_price: return
    
    # FILTER-LOGIK: Preis > EMA UND RSI >= Threshold
    is_uptrend = current_price > ema
    is_overbought = rsi >= RSI_THRESHOLD
    
    if is_uptrend and is_overbought:
        qty = round(TRADE_SIZE / current_price, 6)
        ts = str(int(time.time() * 1000))
        
        print(f"[ENTRY LONG] {symbol} @ {current_price} | RSI({RSI_TIMEFRAME}:{RSI_PERIOD}): {rsi:.1f} | EMA({EMA_TIMEFRAME}:{EMA_PERIOD}): {ema:.2f}")
        
        entry_params = {
            "symbol": symbol, "side": "BUY", "positionSide": "LONG", 
            "type": "MARKET", "quantity": str(qty), "leverage": str(LEVERAGE), "timestamp": ts
        }
        
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(entry_params.items()))}&signature={sign_bingx(entry_params)}", headers={"X-BX-APIKEY": API_KEY})
        
        tp = current_price * (1 + TP_PERCENT / 100)
        sl = current_price * (1 - SL_PERCENT / 100)
        time.sleep(1)
        set_tp_sl(symbol, qty, tp, sl)
    else:
        reason = f"Trend ({EMA_TIMEFRAME}:{EMA_PERIOD}) negativ" if not is_uptrend else f"RSI ({RSI_TIMEFRAME}:{RSI_PERIOD}) {rsi:.1f} < {RSI_THRESHOLD}"
        print(f"[SKIP] {symbol}: {reason}")

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