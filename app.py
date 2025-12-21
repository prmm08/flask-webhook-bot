# -------- V 4.7: BINGX FUTURES - SHORT WENN RSI >= 80 UND OPEN INTEREST < Threshold (COINGLASS FIX) --------

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
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY") 
# Basis URL ohne Endpunkt
COINGLASS_BASE_URL = "https://fapi.coinglass.com" 

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# Globale Settings für die Short-Strategie
RSI_TIMEFRAME = "1m"
TP_PERCENT, SL_PERCENT, BE_PERCENT = 3.0, 1.5, 3.0
TRADE_SIZE = 10    # USDT Einsatz
LEVERAGE = 10
# Schwellenwert in USD, z.B. 10 Millionen
OI_THRESHOLD_USDT = 10000000.0 

order_lock = threading.Lock()

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

def get_ohlcv(symbol, interval, limit=100):
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

# ---------------- OPEN INTEREST LOGIC (COINGLASS FIX) ----------------

def get_open_interest(symbol) -> float:
    if not COINGLASS_API_KEY:
        print("[FEHLER] COINGLASS_API_KEY ist nicht gesetzt. OI=-1.")
        return -1.0 
        
    # Symbole konvertieren: FET-USDT -> FET
    base_currency = symbol.replace('-USDT', '')
    
    try:
        # URL korrekt zusammensetzen
        url = f"{COINGLASS_BASE_URL}/api/v1/futures/openInterest"
        headers = {"coinglassSecret": COINGLASS_API_KEY}
        params = {"symbol": base_currency} 
        
        r = requests.get(url, headers=headers, params=params, timeout=10).json()
        
        if r.get("code") == "00000":
            data = r.get("data", {}).get("list", [])
            if data:
                oi_value = float(data[-1].get("openInterest"))
                return oi_value
            else:
                print(f"[FEHLER] CoinGlass API: Keine Daten im 'list' Array für {symbol}. Code: {r.get('code')}. OI=-1.")
                return -1.0
        else:
            print(f"[FEHLER] CoinGlass API Code: {r.get('code')}, Message: {r.get('msg')}. OI=-1.")
            return -1.0

    except requests.exceptions.RequestException as e:
        # Hier sollte der "No scheme supplied" Fehler behoben sein
        print(f"[FEHLER] Netzwerk- oder Request-Problem beim Abrufen von CoinGlass: {e}. OI=-1.")
        return -1.0
    except Exception as e:
        print(f"[FEHLER] Unerwarteter Fehler beim Parsen der CoinGlass Daten: {e}. OI=-1.")
        return -1.0

# ---------------- POSITION ACTIONS ----------------

def has_active_position(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        for p in r.get("data", []):
             if abs(float(p.get("positionAmt", 0))) > 0: return True
        return False
    except: return False

def close_position_market(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = sign_bingx(params)
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions?{qs}&signature={sig}"
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print(f"[EXIT] {symbol} Markt-Close ausgeführt.")

def set_tp_sl(symbol, qty, tp_price, sl_price):
    exit_side = "BUY"
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": exit_side, "positionSide": "SHORT",
            "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}", headers={"X-BX-APIKEY": API_KEY})
    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- MONITORING ----------------
def monitor_trade(symbol, entry, tp, sl, be_trigger):
    be_active = False
    while has_active_position(symbol):
        curr = get_price_bingx(symbol)
        if not curr: time.sleep(2); continue
        if not be_active and curr <= be_trigger: be_active = True
        if be_active and curr >= entry: close_position_market(symbol); break
        if curr <= tp or curr >= sl: close_position_market(symbol); break
        time.sleep(3)
    print(f"[MONITOR] Ende für {symbol}")


# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol, timeframe):
    with order_lock:
        if has_active_position(symbol):
            print(f"[ABORT] Position für {symbol} existiert bereits.")
            return
            
        # 1. RSI Check
        ohlcv_asset = get_ohlcv(symbol, timeframe)
        if not ohlcv_asset: return
        rsi = calc_rsi([float(c["close"]) for c in ohlcv_asset])

        # 2. Open Interest Check
        open_interest_usdt = get_open_interest(symbol) 
        
        # 3. Filterbedingungen prüfen
        # Prüfe: RSI >= 80 UND OI gültig (>0) UND OI < Schwellenwert
        if rsi >= 80 and open_interest_usdt > 0 and open_interest_usdt < OI_THRESHOLD_USDT:
            price = get_price_bingx(symbol)
            if not price: return
            
            qty = round(TRADE_SIZE / price, 6)
            
            print(f"[ENTRY] SHORT {symbol} | TF: {timeframe} | RSI: {rsi:.1f} | OI: ${open_interest_usdt:,.0f}")

            ts = str(int(time.time() * 1000))
            entry_p = {"symbol": symbol, "side": "SELL", "positionSide": "SHORT", "type": "MARKET", "quantity": str(qty), "leverage": str(LEVERAGE), "timestamp": ts}
            r = requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(entry_p.items()))}&signature={sign_bingx(entry_p)}", headers={"X-BX-APIKEY": API_KEY}).json()

            if r.get("code") == 0:
                time.sleep(2)
                tp = price * (1 - TP_PERCENT / 100)
                sl = price * (1 + SL_PERCENT / 100)
                be_trigger = price * (1 - BE_PERCENT / 100)
                set_tp_sl(symbol, qty, tp, sl)
                threading.Thread(target=monitor_trade, args=(symbol, price, tp, sl, be_trigger)).start()
            else:
                print(f"[ERROR] Order fehlgeschlagen: {r}")
        
        else:
            print(f"[FILTER] Kein Signal für {symbol}. Bedingungen nicht erfüllt (RSI={rsi:.1f}, OI=${open_interest_usdt:,.0f}).")


# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    tf = data.get("tf", RSI_TIMEFRAME) 
    
    if not currency: 
        return jsonify({"status": "no_currency"}), 400
    
    symbol = f"{currency}-USDT"
    
    threading.Thread(target=execute_trade_bingx, args=(symbol, tf)).start()
    
    return jsonify({"status": "processing", "symbol": symbol}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
