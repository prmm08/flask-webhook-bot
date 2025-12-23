# -------- V 3.5: BINGX FUTURES - VERBESSERTES LOGGING/FEHLERANALYSE --------

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

# Setup Logging, um Flask's Output besser zu sehen
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# --- Strategie Settings ---
RSI_TIMEFRAME = "1m"
RSI_PERIOD = 14
RSI_THRESHOLD = 75

EMA_TIMEFRAME = "5m"
EMA_PERIOD = 50

LEVERAGE = 10
TRADE_SIZE = 10
TP_PERCENT, SL_PERCENT, BE_PERCENT = 1.0, 1.5, 0.5 

# ---------------- SIGNING & HELPERS ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def send_signed_request(method, endpoint, params, headers=None):
    """Zentralisierte Funktion für API Requests mit Logging."""
    try:
        url = f"{BINGX_BASE}{endpoint}"
        
        # Signatur berechnen
        if 'timestamp' in params:
            params["signature"] = sign_bingx(params)
            qs = urllib.parse.urlencode(sorted(params.items()))
            full_url = f"{url}?{qs}"
        else:
            # Für GET/Price Endpunkte ohne Signatur
            full_url = f"{url}?{urllib.parse.urlencode(params)}"
        
        # Headers für authentifizierte Requests
        request_headers = {"X-BX-APIKEY": API_KEY}
        if headers:
            request_headers.update(headers)

        logging.info(f"Sending Request: {method} {full_url}")
        
        if method == "POST":
            r = requests.post(full_url, headers=request_headers, timeout=10)
        elif method == "GET":
            r = requests.get(full_url, headers=request_headers, timeout=10)
        
        # Logge die rohe Antwort
        logging.info(f"API Response ({endpoint}): {r.status_code} - {r.text}")
        r.raise_for_status() # Löst Ausnahme bei 4xx/5xx Fehlern aus
        return r.json()

    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP Error for {endpoint}: {e.response.text}")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Network Error for {endpoint}: {e}")
        return None

def get_price_bingx(symbol):
    r = send_signed_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if r and r.get("code") == 0:
        return float(r["data"]["price"])
    return None

def get_ohlcv(symbol, interval="1m", limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = send_signed_request("GET", "/openApi/swap/v2/quote/klines", params)
    if r and r.get("code") == 0:
        return r.get("data", [])
    return []

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
    if not closes: return 0
    if len(closes) < 2: return closes[-1]
    alpha = 2 / (period + 1)
    current_ema = closes[0]
    for price in closes[1:]:
        current_ema = (price * alpha) + (current_ema * (1 - alpha))
    return current_ema

# ---------------- POSITION ACTIONS ----------------

def has_active_position(symbol):
    ts = str(int(time.time() * 1000))
    r = send_signed_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol, "timestamp": ts})
    if r and r.get("code") == 0:
        for p in r.get("data", []):
             if abs(float(p.get("positionAmt", 0))) > 0 and p.get("positionSide") == "LONG": return True
    return False

def set_tp_sl(symbol, qty, tp_price, sl_price):
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "SELL", "positionSide": "LONG",
            "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        # Nutzt die neue, loggende Request-Funktion
        send_signed_request("POST", "/openApi/swap/v2/trade/order", params)

    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- MONITORING BE ----------------

def monitor_be(symbol, entry_price, be_trigger_price):
    be_active = False
    while has_active_position(symbol):
        curr = get_price_bingx(symbol)
        if not curr: time.sleep(2); continue

        if not be_active and curr >= be_trigger_price:
            be_active = True
            logging.info(f"[BE STATUS] {symbol} Profit erreicht. Ziehe SL auf Entry ({entry_price}) nach.")
            
            # 1. Alle Stop-Orders stornieren
            ts_cancel = str(int(time.time() * 1000))
            send_signed_request("POST", "/openApi/swap/v2/trade/cancelAllOpenOrders", {"symbol": symbol, "timestamp": ts_cancel})
            time.sleep(1)

            # 2. Aktuelle Positionsmenge holen und neue SL setzen
            ts_pos = str(int(time.time() * 1000))
            r_pos = send_signed_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol, "timestamp": ts_pos})
            
            qty = 0
            if r_pos and r_pos.get("code") == 0:
                 for p in r_pos.get("data", []):
                      if abs(float(p.get("positionAmt", 0))) > 0 and p.get("positionSide") == "LONG": 
                          qty = float(p.get("positionAmt"))
                          break
            
            if qty > 0:
                ts_new_sl = str(int(time.time() * 1000))
                params_new_sl = {
                    "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                    "type": "STOP_MARKET", "quantity": str(qty), "stopPrice": "{:.6f}".format(entry_price),
                    "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts_new_sl
                }
                send_signed_request("POST", "/openApi/swap/v2/trade/order", params_new_sl)
                logging.info(f"[BE SUCCESS] Neuer SL bei Entry Price {entry_price} gesetzt.")
            break 
        time.sleep(3)

# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol):
    if has_active_position(symbol): return
            
    ohlcv_rsi = get_ohlcv(symbol, RSI_TIMEFRAME, limit=RSI_PERIOD + 5)
    if not ohlcv_rsi: return
    closes_rsi = [float(c["close"]) for c in ohlcv_rsi]
    rsi = calc_rsi(closes_rsi, RSI_PERIOD)
    
    ohlcv_ema = get_ohlcv(symbol, EMA_TIMEFRAME, limit=EMA_PERIOD + 5)
    if not ohlcv_ema: return
    closes_ema = [float(c["close"]) for c in ohlcv_ema]
    ema = calc_ema(closes_ema, EMA_PERIOD)
    
    current_price = get_price_bingx(symbol)
    if not current_price: return
    
    if current_price > ema and rsi >= RSI_THRESHOLD:
        qty = round(TRADE_SIZE / current_price, 6)
        
        logging.info(f"[ENTRY LONG] {symbol} | RSI: {rsi:.1f} | EMA: {ema:.2f}")
        
        entry_params = {
            "symbol": symbol, "side": "BUY", "positionSide": "LONG", 
            "type": "MARKET", "quantity": str(qty), "leverage": str(LEVERAGE), 
            "timestamp": str(int(time.time() * 1000))
        }
        
        send_signed_request("POST", "/openApi/swap/v2/trade/order", entry_params)
        
        tp = current_price * (1 + TP_PERCENT / 100)
        sl = current_price * (1 - SL_PERCENT / 100)
        be_trigger = current_price * (1 + BE_PERCENT / 100)

        time.sleep(2) # Pause für Orderverarbeitung
        set_tp_sl(symbol, qty, tp, sl)
        threading.Thread(target=monitor_be, args=(symbol, current_price, be_trigger)).start()
        
    else:
        reason = f"Trend ({EMA_TIMEFRAME}:{EMA_PERIOD}) negativ" if not is_uptrend else f"RSI ({RSI_TIMEFRAME}:{RSI_PERIOD}) {rsi:.1f} < {RSI_THRESHOLD}"
        logging.info(f"[SKIP] {symbol}: {reason}")

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
